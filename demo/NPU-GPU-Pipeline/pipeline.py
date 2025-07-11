#
# Copyright © 2023 Advanced Micro Devices, Inc. All rights reserved.
#
import torch
import torch.nn as nn
import onnxruntime
import numpy as np
import argparse
from utils_img2img import (
    LoadImages,
    non_max_suppression,
    plot_images,
    output_to_target,
    preprocess, post_process
)
import sys
import pathlib

CURRENT_DIR = pathlib.Path(__file__).parent
sys.path.append(str(CURRENT_DIR))
from data.data_tiling import tiling_inference
import os
import threading
import queue
import cv2
import time

sys.path.append("./stable_diffusion")
from pathlib import Path
from typing import Dict
import config
from packaging import version
from olive.common.utils import set_tempdir
from olive.workflows import run as olive_run
import tkinter as tk
from tkinter import font
from PIL import Image, ImageTk
from power_utils_filtered import *
import random
from ort_util_img2img import get_ort_pipeline_sd
import collections
from npu_gpu_utils import get_apu_info

def make_parser():
    parser = argparse.ArgumentParser("onnxruntime inference sample")
    parser.add_argument(
        "-i",
        "--image_path",
        type=str,
        default="./test.mp4",
        help="path to your input image or video.",
    )
    parser.add_argument(
        "-o",
        "--output_path",
        type=str,
        default="./demo_infer.jpg",
        help="path to your output directory.",
    )
    parser.add_argument(
        "--npu", action="store_true", help="flag to enable off-loading CNNs to NPU"
    )
    parser.add_argument(
        "--power", action="store_true", help="flag to enable power measurements"
    )
    parser.add_argument(
        "--igpu",
        action="store_true",
        help="flag to enable off-loading Stable Diffusion to iGPU",
    )
    parser.add_argument(
        "--provider_config", default="", type=str, help="provider config for ryzen ai"
    )
    return parser


classnames = [
    "person",
    "bicycle",
    "car",
    "motorcycle",
    "airplane",
    "bus",
    "train",
    "truck",
    "boat",
    "traffic light",
    "fire hydrant",
    "stop sign",
    "parking meter",
    "bench",
    "bird",
    "cat",
    "dog",
    "horse",
    "sheep",
    "cow",
    "elephant",
    "bear",
    "zebra",
    "giraffe",
    "backpack",
    "umbrella",
    "handbag",
    "tie",
    "suitcase",
    "frisbee",
    "skis",
    "snowboard",
    "sports ball",
    "kite",
    "baseball bat",
    "baseball glove",
    "skateboard",
    "surfboard",
    "tennis racket",
    "bottle",
    "wine glass",
    "cup",
    "fork",
    "knife",
    "spoon",
    "bowl",
    "banana",
    "apple",
    "sandwich",
    "orange",
    "broccoli",
    "carrot",
    "hot dog",
    "pizza",
    "donut",
    "cake",
    "chair",
    "couch",
    "potted plant",
    "bed",
    "dining table",
    "toilet",
    "tv",
    "laptop",
    "mouse",
    "remote",
    "keyboard",
    "cell phone",
    "microwave",
    "oven",
    "toaster",
    "sink",
    "refrigerator",
    "book",
    "clock",
    "vase",
    "scissors",
    "teddy bear",
    "hair drier",
    "toothbrush",
]
names = {k: classnames[k] for k in range(80)}
imgsz = [640, 640]
detected_objects_queue = queue.Queue()
stop_event_sres = threading.Event()
stop_event_detect = threading.Event()
stop_event_sd = threading.Event()
sd_input = set()
sd_input_queue = queue.Queue()
sd_input_set = set()
detected_objects_dict = {}

### Stable Diffusion Parameters ###
sd_model_id = "runwayml/stable-diffusion-v1-5"
sd_num_images = 1
sd_batch_size = 1
sd_image_size = 384
sd_num_inference_steps = 50
sd_guidance_scale = 7.5
sd_strength = 1.0
sd_provider = "cpu"
sd_count = 0
generated_images = []
generated_prompts = []
sr_images = []
sr_input_dict = collections.defaultdict(int)
od_output_set = set()
### Stable Diffusion Parameters ###


def run_pipeline_sd(
    pipeline,
    prompt,
    negative_prompt,
    init_image,
    num_images,
    batch_size,
    image_size,
    num_inference_steps,
    guidance_scale,
    strength: float,
    provider: str,
    image_callback=None,
    step_callback=None,
):
    print(f"\nInference Batch Start (batch size = {batch_size}).")
    kwargs = {}
    result = pipeline(
        [prompt] * batch_size,
        negative_prompt=[negative_prompt],
        image=init_image,
        num_inference_steps=num_inference_steps,
        callback=None,
        guidance_scale=guidance_scale,
        generator=np.random.RandomState(45),
        **kwargs,
    )
    return result.images[0]


def display_images_sd(generated_images, prompt):
    generated_images = [Image.fromarray(np.uint8(img)) for img in generated_images]
    total_width = sum(img.width for img in generated_images)
    max_height = max(img.height for img in generated_images)
    text_height = 50

    root = tk.Toplevel()
    root.title("Image generated by Stable Diffusion, enhanced by RCAN (super res)")
    canvas = tk.Canvas(root, width=total_width, height=max_height + text_height)
    canvas.pack()
    my_font = font.Font(weight="bold", size=12)

    tk_images = [ImageTk.PhotoImage(img) for img in generated_images]
    x_offset = 0
    for tk_image in tk_images:
        canvas.create_image(x_offset, 0, anchor="nw", image=tk_image)
        x_offset += tk_image.width()
    canvas.create_text(
        0,
        tk_images[0].height() + 8,
        anchor="nw",
        text="style transfer: " + prompt,
        font=my_font,
        fill="black",
        width=tk_images[0].width(),
    )
    root.mainloop()


def display_images_sr(sr_images):
    sr_images = [
        Image.fromarray(np.uint8(cv2.cvtColor(img, cv2.COLOR_BGR2RGB)))
        for img in sr_images
    ]
    images_per_row = 5
    total_widths = [sum(img.width for img in sr_images[i:i+images_per_row]) for i in range(0, len(sr_images), images_per_row)]
    total_heights = [max(img.height for img in sr_images[i:i+images_per_row]) for i in range(0, len(sr_images), images_per_row)]
    max_width = max(total_widths)
    total_height = sum(total_heights)
    root = tk.Tk()
    root.title("Detected objects after super resolution")
    canvas = tk.Canvas(root, width=max_width, height=total_height)
    canvas.pack()

    tk_images = [ImageTk.PhotoImage(img) for img in sr_images]
    x_offset = 0
    y_offset = 0
    for i, tk_image in enumerate(tk_images):
        canvas.create_image(x_offset, y_offset, anchor="nw", image=tk_image)
        x_offset += tk_image.width()
        if (i + 1) % images_per_row == 0:
            x_offset = 0
            y_offset += total_heights[i // images_per_row]
    root.mainloop()


def display_video(source, dataset, video_filename):
    cap = cv2.VideoCapture(source)
    source_fps = cap.get(cv2.CAP_PROP_FPS)
    cap.release()
    out_video = cv2.VideoWriter(
        video_filename,
        cv2.VideoWriter_fourcc("m", "p", "4", "v"),
        source_fps,
        (640, 640),
    )
    for i in range(dataset.frame):
        img = detected_objects_dict[i]
        out_video.write(cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR))
    out_video.release()

    wait_time = int(1000 / (1.2 * source_fps))
    cap = cv2.VideoCapture(video_filename)
    while cap.isOpened():
        ret, frame = cap.read()
        if ret:
            cv2.imshow("Video", frame)
            if cv2.waitKey(wait_time) & 0xFF == ord("q"):
                break
        else:
            break

    cap.release()


def object_detection_worker(task_queue, onnx_runtime_session_1):
    while not stop_event_detect.is_set():
        try:
            batch_no, im = task_queue.get(block=False)
            if batch_no == 0:
                print("Processing frames for object detection")

        except queue.Empty:
            stop_event_detect.set()
            detected_objects_queue.put(None)
            print("Task queue empty")
            break
        im = preprocess(im)
        if len(im.shape) == 3:
            im = im[None]
        outputs = onnx_runtime_session_1.run(
            None,
            {
                onnx_runtime_session_1.get_inputs()[0]
                .name: im.permute(0, 2, 3, 1)
                .cpu()
                .numpy()
            },
        )
        outputs = [torch.tensor(item).permute(0, 3, 1, 2) for item in outputs]

        preds = post_process(outputs)
        preds = non_max_suppression(
            preds, 0.25, 0.7, agnostic=False, max_det=300, classes=None
        )
        res, boxes, cropped_imgs, op_labels = plot_images(
            im,
            *output_to_target(preds, max_det=15),
            source,
            names=names,
        )
        detected_objects_dict[batch_no] = res
        if batch_no%5==0:
            detected_objects_queue.put(
                (res, boxes, batch_no, cropped_imgs, op_labels)
            )
        if int(batch_no) == 0:
            sd_input_queue.put(op_labels)
    print("Object detection worker is done.")


def super_resolution_worker(onnx_runtime_session_2):
    while not stop_event_sres.is_set():
        try:
            item = detected_objects_queue.get()
        except queue.Empty:
            stop_event_sres.set()
            print("Detection queue empty")
            break
        if item is None:
            stop_event_sres.set()
            break
        res, boxes, batch_no, cropped_imgs, op_labels = item
        labels = []
        for img, label in cropped_imgs:
            lr = img[np.newaxis, :, :, :].transpose((0, 3, 1, 2)).astype(np.float32)
            if sr_input_dict[label] >= 5: 
                continue
            try:
                sr = tiling_inference(onnx_runtime_session_2, lr, 8, (56, 56))
                sr = np.clip(sr, 0, 255)
                sr = sr.squeeze().transpose((1, 2, 0)).astype(np.uint8)
                sr = cv2.cvtColor(sr, cv2.COLOR_BGR2RGB)
                sr_input_dict[label]+=1
                processed_folder = "./results/sr_images/" + label + "/"
                if not os.path.exists(processed_folder):
                    os.makedirs(processed_folder)
                filename = os.path.join(processed_folder, str(sr_input_dict[label]) + ".png")
                cv2.imwrite(filename, sr)
                sr_images.append(sr)
                print("Finished super resolution on a frame.")
            except Exception as e:
                # print(f"Error processing {filename}: {e}")
                pass
        detected_objects_queue.task_done()
    print("Super resolution worker is done.")


def final_sr(onnx_runtime_session_2, image_path):
    img = cv2.imread(image_path)
    lr = img[np.newaxis, :, :, :].transpose((0, 3, 1, 2)).astype(np.float32)
    try:
        sr = tiling_inference(onnx_runtime_session_2, lr, 8, (56, 56))
        sr = np.clip(sr, 0, 255)
        sr = sr.squeeze().transpose((1, 2, 0)).astype(np.uint8)
        sr = cv2.cvtColor(sr, cv2.COLOR_BGR2RGB)
        filename = os.path.join("./results/sd_img2img/enhanced_sd.png")
        cv2.imwrite(filename, cv2.cvtColor(sr, cv2.COLOR_BGR2RGB))
        return sr
    except:
        pass


def stable_diffusion_worker(sd_pipeline, init_image):
    while not stop_event_sd.is_set():
        try:
            objects = sd_input_queue.get(timeout=10)
            start_prompt = ""
            for obj in objects:
                start_prompt += obj + " "
        except queue.Empty:
            print("Prompt queue empty")
            break
        if len(sd_input) == 1:
            stop_event_sd.set()
            break
        prompt = (
            start_prompt
            + "in a beach, sand, water, high definition, realistic, detailed and intricate"
        )
        negative_prompt = "animated"
        img = run_pipeline_sd(
            sd_pipeline,
            prompt,
            negative_prompt,
            init_image,
            sd_num_images,
            sd_batch_size,
            sd_image_size,
            sd_num_inference_steps,
            sd_guidance_scale,
            sd_strength,
            provider=sd_provider,
        )
        generated_images.append(img)
        generated_prompts.append(prompt)
        res_folder = "./results/sd_img2img/"
        if not os.path.exists(res_folder):
            os.makedirs(res_folder)
        op_path = res_folder + "sd_result.png"
        img.save(op_path)
        print("Finished generating image for a prompt.")
        sd_input.add(prompt)
    print("Stable Diffusion worker is done.")


if __name__ == "__main__":
    random.seed(45)
    np.random.seed(45)
    torch.manual_seed(45)
    args = make_parser().parse_args()
    num_threads = 4 if args.npu else 1
    os.environ["NUM_OF_DPU_RUNNERS"] = str(num_threads)
    source = args.image_path
    dataset = LoadImages(
        source, imgsz=imgsz, stride=32, auto=False, transforms=None, vid_stride=1
    )
    task_queue = queue.Queue()

    script_dir = Path(__file__).resolve().parent
    if args.igpu:
        model_dir_name = f"optimized-dml"
        sd_provider = "dml"
    else:
        model_dir_name = f"unoptimized"
    sd_model_dir = (
        script_dir / "stable_diffusion" / "models" / model_dir_name / sd_model_id
    )

    for batch_no, batch in enumerate(dataset):
        path, im, im0s, vid_cap, s = batch
        task_queue.put((batch_no, im))
        if batch_no == 0:
            im_pil = Image.fromarray(np.transpose(im, (1, 2, 0)))
            im_pil_resized = im_pil.resize((sd_image_size, sd_image_size))
            im_pil_resized.save("init_image.png")

    print("Number of frames = ", dataset.frames)
    if args.npu:
        npu_device = get_apu_info()
        print('RYZEN_AI_INSTALLATION_PATH:', os.environ["RYZEN_AI_INSTALLATION_PATH"])

        xclbin_path = ''
        if npu_device == 'STX':
          xclbin_path = '{}\\voe-4.0-win_amd64\\xclbins\\strix\\AMD_AIE2P_4x4_Overlay.xclbin'.format(os.environ["RYZEN_AI_INSTALLATION_PATH"])
        if npu_device == 'PHX':
          xclbin_path = '{}\\voe-4.0-win_amd64\\xclbins\\phoenix\\4x4.xclbin'.format(os.environ["RYZEN_AI_INSTALLATION_PATH"])
        print('XCLBIN_PATH:', xclbin_path)

        providers = ["VitisAIExecutionProvider"]
        #cache_dir = Path(__file__).parent.resolve()
        provider_options = [{"config_file": args.provider_config,
                             "xclbin": xclbin_path}]

    else:
        providers = ["CPUExecutionProvider"]
        provider_options = [{}]

    onnx_model = onnxruntime.InferenceSession(
        "yolov8m.onnx", providers=providers, provider_options=provider_options
    )
    ort_session_rcan = onnxruntime.InferenceSession(
        "RCAN_int8_NHWC.onnx", providers=providers, provider_options=provider_options
    )
    thread_pool = []

    pipeline_pool = []
    pipeline_pool.append(
        get_ort_pipeline_sd(
            sd_model_dir, sd_batch_size, sd_image_size, sd_provider, sd_guidance_scale
        )
    )
    for i in range(len(pipeline_pool)):
        thread_pool.append(
            threading.Thread(
                target=stable_diffusion_worker, args=(pipeline_pool[i], im_pil_resized)
            )
        )
    for i in range(num_threads):
        thread_pool.append(
            threading.Thread(
                target=object_detection_worker, args=(task_queue, onnx_model)
            )
        )
    thread_pool.append(
        threading.Thread(target=super_resolution_worker, args=(ort_session_rcan,))
    )

    start = time.perf_counter()
    if args.power:
        powerfilename = StartPowerMeas()
    for thread in thread_pool:
        thread.start()
    for thread in thread_pool:
        thread.join()
    generated_images_enhanced = final_sr(
        ort_session_rcan, "./results/sd_img2img/sd_result.png"
    )
    generated_images.append(generated_images_enhanced)
    end = time.perf_counter()
    print("total inference time = ", (end - start), " seconds")
    if args.power:
        StopPowerMeas()
        median_powers = med_pow(powerfilename)
        print("Power stats: ", median_powers)

    display_video(source, dataset, "detection_results_giraffe_zebra.mp4")
    display_images_sr(sr_images)
    display_images_sd(generated_images, generated_prompts[0])
