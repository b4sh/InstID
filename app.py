import sys
sys.path.append('./')

import os
import cv2
import math
import torch
import random
import numpy as np
import argparse

import PIL
from PIL import Image

import diffusers
from diffusers.utils import load_image
from diffusers.models import ControlNetModel

from huggingface_hub import hf_hub_download

import insightface
from insightface.app import FaceAnalysis

from style_template import styles
from pipeline_stable_diffusion_xl_instantid import StableDiffusionXLInstantIDPipeline
from model_util import load_models_xl, get_torch_device, torch_gc

import gradio as gr

import warnings
warnings.filterwarnings('ignore', category=UserWarning, message='TypedStorage is deprecated')

# global variable
MAX_SEED = np.iinfo(np.int32).max
device = get_torch_device()
dtype = torch.float16 if str(device).__contains__("cuda") else torch.float32
STYLE_NAMES = list(styles.keys())
DEFAULT_STYLE_NAME = "Watercolor"

# Load face encoder
app = FaceAnalysis(name='antelopev2', root='./', providers=['CPUExecutionProvider'])
app.prepare(ctx_id=0, det_size=(640, 640))

# Path to InstantID models
face_adapter = f'./checkpoints/ip-adapter.bin'
controlnet_path = f'./checkpoints/ControlNetModel'

# Load pipeline
controlnet = ControlNetModel.from_pretrained(controlnet_path, torch_dtype=dtype)

def main(pretrained_model_name_or_path="stablediffusionapi/zavychroma_sdxl"):

    if pretrained_model_name_or_path.endswith(
            ".ckpt"
        ) or pretrained_model_name_or_path.endswith(".safetensors"):
            scheduler_kwargs = hf_hub_download(
                repo_id="stablediffusionapi/zavychroma_sdxl",
                subfolder="scheduler",
                filename="scheduler_config.json",
            )

            (tokenizers, text_encoders, unet, _, vae) = load_models_xl(
                pretrained_model_name_or_path=pretrained_model_name_or_path,
                scheduler_name=None,
                weight_dtype=dtype,
            )

            scheduler = diffusers.EulerDiscreteScheduler.from_config(scheduler_kwargs)
            pipe = StableDiffusionXLInstantIDPipeline(
                vae=vae,
                text_encoder=text_encoders[0],
                text_encoder_2=text_encoders[1],
                tokenizer=tokenizers[0],
                tokenizer_2=tokenizers[1],
                unet=unet,
                scheduler=scheduler,
                controlnet=controlnet,
            ).to(device)

    else:
        pipe = StableDiffusionXLInstantIDPipeline.from_pretrained(
            pretrained_model_name_or_path,
            controlnet=controlnet,
            torch_dtype=dtype,
            feature_extractor=None,
        ).to(device)
        
        pipe.scheduler = diffusers.EulerDiscreteScheduler.from_config(pipe.scheduler.config)

    pipe.load_ip_adapter_instantid(face_adapter)
    pipe.enable_vae_slicing()
    pipe.enable_xformers_memory_efficient_attention()    
    
    def randomize_seed_fn(seed: int, randomize_seed: bool) -> int:
        if randomize_seed:
            seed = random.randint(0, MAX_SEED)
        return seed

    def swap_to_gallery(images):
        return gr.update(value=images, visible=True), gr.update(visible=True), gr.update(visible=False)

    def upload_example_to_gallery(images, prompt, style, negative_prompt):
        return gr.update(value=images, visible=True), gr.update(visible=True), gr.update(visible=False)

    def remove_back_to_files():
        return gr.update(visible=False), gr.update(visible=False), gr.update(visible=True)

    def remove_tips():
        return gr.update(visible=False)

    def convert_from_cv2_to_image(img: np.ndarray) -> Image:
        return Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))

    def convert_from_image_to_cv2(img: Image) -> np.ndarray:
        return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)

    def draw_kps(image_pil, kps, color_list=[(255,0,0), (0,255,0), (0,0,255), (255,255,0), (255,0,255)]):
        stickwidth = 4
        limbSeq = np.array([[0, 2], [1, 2], [3, 2], [4, 2]])
        kps = np.array(kps)

        w, h = image_pil.size
        out_img = np.zeros([h, w, 3])

        for i in range(len(limbSeq)):
            index = limbSeq[i]
            color = color_list[index[0]]

            x = kps[index][:, 0]
            y = kps[index][:, 1]
            length = ((x[0] - x[1]) ** 2 + (y[0] - y[1]) ** 2) ** 0.5
            angle = math.degrees(math.atan2(y[0] - y[1], x[0] - x[1]))
            polygon = cv2.ellipse2Poly((int(np.mean(x)), int(np.mean(y))), (int(length / 2), stickwidth), int(angle), 0, 360, 1)
            out_img = cv2.fillConvexPoly(out_img.copy(), polygon, color)
        out_img = (out_img * 0.6).astype(np.uint8)

        for idx_kp, kp in enumerate(kps):
            color = color_list[idx_kp]
            x, y = kp
            out_img = cv2.circle(out_img.copy(), (int(x), int(y)), 10, color, -1)

        out_img_pil = Image.fromarray(out_img.astype(np.uint8))
        return out_img_pil

    def resize_img(input_image, max_side=1024, min_side=1024, size=None, 
                pad_to_max_side=False, mode=PIL.Image.Resampling.BILINEAR, base_pixel_number=64):

            w, h = input_image.size
            if size is not None:
                w_resize_new, h_resize_new = size
            else:
                ratio = min_side / min(h, w)
                w, h = round(ratio*w), round(ratio*h)
                ratio = max_side / max(h, w)
                input_image = input_image.resize([round(ratio*w), round(ratio*h)], mode)
                w_resize_new = (round(ratio * w) // base_pixel_number) * base_pixel_number
                h_resize_new = (round(ratio * h) // base_pixel_number) * base_pixel_number
            input_image = input_image.resize([w_resize_new, h_resize_new], mode)

            if pad_to_max_side:
                res = np.ones([max_side, max_side, 3], dtype=np.uint8) * 255
                offset_x = (max_side - w_resize_new) // 2
                offset_y = (max_side - h_resize_new) // 2
                res[offset_y:offset_y+h_resize_new, offset_x:offset_x+w_resize_new] = np.array(input_image)
                input_image = Image.fromarray(res)
            return input_image

    def apply_style(style_name: str, positive: str, negative: str = "") -> tuple[str, str]:
        p, n = styles.get(style_name, styles[DEFAULT_STYLE_NAME])
        return p.replace("{prompt}", positive), n + ' ' + negative

    def generate_image(face_image, pose_image, prompt, negative_prompt, style_name, num_steps, identitynet_strength_ratio, adapter_strength_ratio, guidance_scale, seed, progress=gr.Progress(track_tqdm=True)):
        if face_image is None:
            raise gr.Error(f"Cannot find any input face image! Please upload the face image")
        
        if prompt is None:
            prompt = "a person"
        
        # apply the style template
        prompt, negative_prompt = apply_style(style_name, prompt, negative_prompt)
        
        face_image = load_image(face_image[0])
        face_image = resize_img(face_image)
        face_image_cv2 = convert_from_image_to_cv2(face_image)
        height, width, _ = face_image_cv2.shape
        
        # Extract face features
        face_info = app.get(face_image_cv2)
        
        if len(face_info) == 0:
            raise gr.Error(f"Cannot find any face in the image! Please upload another person image")
        
        face_info = sorted(face_info, key=lambda x:(x['bbox'][2]-x['bbox'][0])*x['bbox'][3]-x['bbox'][1])[-1]  # only use the maximum face
        face_emb = face_info['embedding']
        face_kps = draw_kps(convert_from_cv2_to_image(face_image_cv2), face_info['kps'])
        
        if pose_image is not None:
            pose_image = load_image(pose_image[0])
            pose_image = resize_img(pose_image)
            pose_image_cv2 = convert_from_image_to_cv2(pose_image)
            
            face_info = app.get(pose_image_cv2)
            
            if len(face_info) == 0:
                raise gr.Error(f"Cannot find any face in the reference image! Please upload another person image")
            
            face_info = face_info[-1]
            face_kps = draw_kps(pose_image, face_info['kps'])
            
            width, height = face_kps.size
        
        with torch.no_grad():
            generator = torch.Generator(device=device).manual_seed(seed)
        
        print("Start inference...")
        print(f"[Debug] Prompt: {prompt}, \n[Debug] Neg Prompt: {negative_prompt}")
        
        pipe.set_ip_adapter_scale(adapter_strength_ratio)
        
        images = pipe(
            prompt=prompt,
            negative_prompt=negative_prompt,
            image_embeds=face_emb,
            image=face_kps,
            controlnet_conditioning_scale=float(identitynet_strength_ratio),
            num_inference_steps=num_steps,
            guidance_scale=guidance_scale,
            height=height,
            width=width,
            generator=generator
        ).images

        return images, gr.update(visible=True)
        
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    ### Description
    title = r"""
    <h1 align="center">InstantID: Zero-shot Identity-Preserving Generation in Seconds</h1>
    """

    tips = r"""
    ### Usage tips of InstantID
    1. If you're not satisfied with the similarity, try to increase the weight of "IdentityNet Strength" and "Adapter Strength".
    2. If you feel that the saturation is too high, first decrease the Adapter strength. If it is still too high, then decrease the IdentityNet strength.
    3. If you find that text control is not as expected, decrease Adapter strength.
    4. If you find that realistic style is not good enough, go for our Github repo and use a more realistic base model.
    """

    css = '''
    .gradio-container {width: 85% !important}
    '''
    with gr.Blocks(css=css) as demo:

        # description
        gr.Markdown(title)

        with gr.Row():
            with gr.Column():
                
                # upload face image
                face_files = gr.Files(
                            label="Upload a photo of your face",
                            file_types=["image"]
                        )
                uploaded_faces = gr.Gallery(label="Your images", visible=False, columns=1, rows=1, height=512)
                with gr.Column(visible=False) as clear_button_face:
                    remove_and_reupload_faces = gr.ClearButton(value="Remove and upload new ones", components=face_files, size="sm")
                
                # optional: upload a reference pose image
                pose_files = gr.Files(
                            label="Upload a reference pose image (optional)",
                            file_types=["image"]
                        )
                uploaded_poses = gr.Gallery(label="Your images", visible=False, columns=1, rows=1, height=512)
                with gr.Column(visible=False) as clear_button_pose:
                    remove_and_reupload_poses = gr.ClearButton(value="Remove and upload new ones", components=pose_files, size="sm")
                
                # prompt
                prompt = gr.Textbox(label="Prompt",
                        info="Give simple prompt is enough to achieve good face fidelity",
                        placeholder="A photo of a person",
                        value="")
                
                submit = gr.Button("Submit", variant="primary")
                
                style = gr.Dropdown(label="Style template", choices=STYLE_NAMES, value=DEFAULT_STYLE_NAME)
                
                # strength
                identitynet_strength_ratio = gr.Slider(
                    label="IdentityNet strength (for fidelity)",
                    minimum=0,
                    maximum=1.5,
                    step=0.05,
                    value=0.70,
                )
                adapter_strength_ratio = gr.Slider(
                    label="Image adapter strength (for detail)",
                    minimum=0,
                    maximum=1.5,
                    step=0.05,
                    value=0.60,
                )
                
                with gr.Accordion(open=False, label="Advanced Options"):
                    negative_prompt = gr.Textbox(
                        label="Negative Prompt", 
                        placeholder="low quality",
                        value="(lowres, low quality, worst quality:1.2), (text:1.2), watermark, (frame:1.2), deformed, ugly, deformed eyes, blur, out of focus, blurry, deformed cat, deformed, photo, anthropomorphic cat, monochrome, pet collar, gun, weapon, blue, 3d, drones, drone, buildings in background, green",
                    )
                    num_steps = gr.Slider( 
                        label="Number of sample steps",
                        minimum=20,
                        maximum=100,
                        step=1,
                        value=22,
                    )
                    guidance_scale = gr.Slider(
                        label="Guidance scale",
                        minimum=0.1,
                        maximum=10.0,
                        step=0.1,
                        value=5,
                    )
                    seed = gr.Slider(
                        label="Seed",
                        minimum=0,
                        maximum=MAX_SEED,
                        step=1,
                        value=42,
                    )
                    randomize_seed = gr.Checkbox(label="Randomize seed", value=True)

            with gr.Column():
                gallery = gr.Gallery(label="Generated Images")
                usage_tips = gr.Markdown(label="Usage tips of InstantID", value=tips ,visible=False)

            face_files.upload(fn=swap_to_gallery, inputs=face_files, outputs=[uploaded_faces, clear_button_face, face_files])
            pose_files.upload(fn=swap_to_gallery, inputs=pose_files, outputs=[uploaded_poses, clear_button_pose, pose_files])

            remove_and_reupload_faces.click(fn=remove_back_to_files, outputs=[uploaded_faces, clear_button_face, face_files])
            remove_and_reupload_poses.click(fn=remove_back_to_files, outputs=[uploaded_poses, clear_button_pose, pose_files])

            submit.click(
                fn=remove_tips,
                outputs=usage_tips,            
            ).then(
                fn=randomize_seed_fn,
                inputs=[seed, randomize_seed],
                outputs=seed,
                queue=False,
                api_name=False,
            ).then(
                fn=generate_image,
                inputs=[face_files, pose_files, prompt, negative_prompt, style, num_steps, identitynet_strength_ratio, adapter_strength_ratio, guidance_scale, seed],
                outputs=[gallery, usage_tips]
            )

    demo.launch(share=True)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--pretrained_model_name_or_path", type=str, default="stablediffusionapi/zavychroma_sdxl"
    )
    args = parser.parse_args()

    main(args.pretrained_model_name_or_path)
