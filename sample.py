import gc
import io
import math
import sys

from PIL import Image, ImageOps
import requests
import torch
from torch import nn
from torch.nn import functional as F
from torchvision import transforms
from torchvision.transforms import functional as TF
from tqdm.notebook import tqdm

import numpy as np

from guided_diffusion.script_util import create_model_and_diffusion, model_and_diffusion_defaults

from dalle_pytorch import DiscreteVAE, VQGanVAE

from einops import rearrange
from math import log2, sqrt
from utils import *

import argparse
import pickle

import os

from encoders.modules import BERTEmbedder, MakeCutouts

import clip

# argument parsing

parser = argparse.ArgumentParser()

parser.add_argument('--model_path', type=str, default = 'finetune.pt',
                   help='path to the diffusion model')

parser.add_argument('--kl_path', type=str, default = 'kl-f8.pt',
                   help='path to the LDM first stage model')

parser.add_argument('--bert_path', type=str, default = 'bert.pt',
                   help='path to the LDM first stage model')

parser.add_argument('--text', type = str, required = False, default = '',
                    help='your text prompt')

parser.add_argument('--edit', type = str, required = False,
                    help='path to the image you want to edit (either an image file or .npy containing a numpy array of the image embeddings)')

parser.add_argument('--edit_x', type = int, required = False, default = 0,
                    help='x position of the edit image in the generation frame (need to be multiple of 8)')

parser.add_argument('--edit_y', type = int, required = False, default = 0,
                    help='y position of the edit image in the generation frame (need to be multiple of 8)')

parser.add_argument('--edit_width', type = int, required = False, default = 0,
                    help='width of the edit image in the generation frame (need to be multiple of 8)')

parser.add_argument('--edit_height', type = int, required = False, default = 0,
                    help='height of the edit image in the generation frame (need to be multiple of 8)')

parser.add_argument('--mask', type = str, required = False,
                    help='path to a mask image. white pixels = keep, black pixels = discard. width = image width/8, height = image height/8')

parser.add_argument('--negative', type = str, required = False, default = '',
                    help='negative text prompt')

parser.add_argument('--init_image', type=str, required = False, default = None,
                   help='init image to use')

parser.add_argument('--skip_timesteps', type=int, required = False, default = 0,
                   help='how many diffusion steps are gonna be skipped')

parser.add_argument('--prefix', type = str, required = False, default = '',
                    help='prefix for output files')

parser.add_argument('--num_batches', type = int, default = 1, required = False,
                    help='number of batches')

parser.add_argument('--batch_size', type = int, default = 1, required = False,
                    help='batch size')

parser.add_argument('--width', type = int, default = 256, required = False,
                    help='image size of output (multiple of 8)')

parser.add_argument('--height', type = int, default = 256, required = False,
                    help='image size of output (multiple of 8)')

parser.add_argument('--seed', type = int, default=-1, required = False,
                    help='random seed')

parser.add_argument('--guidance_scale', type = float, default = 5.0, required = False,
                    help='classifier-free guidance scale')

parser.add_argument('--steps', type = int, default = 0, required = False,
                    help='number of diffusion steps')

parser.add_argument('--cpu', dest='cpu', action='store_true')

parser.add_argument('--clip_score', dest='clip_score', action='store_true')

parser.add_argument('--clip_guidance', dest='clip_guidance', action='store_true')

parser.add_argument('--clip_guidance_scale', type = float, default = 150, required = False,
                    help='Controls how much the image should look like the prompt') # may need to use lower value for ddim

parser.add_argument('--cutn', type = int, default = 16, required = False,
                    help='Number of cuts')

parser.add_argument('--ddim', dest='ddim', action='store_true') # turn on to use 50 step ddim

parser.add_argument('--ddpm', dest='ddpm', action='store_true') # turn on to use 50 step ddim

parser.add_argument('--edit_ui', dest='edit_ui', action='store_true') # Use extended inpainting UI

parser.add_argument('--ui_test', dest='ui_test', action='store_true') # Test UI without loading real functionality


args = parser.parse_args()

if args.edit and not args.mask:
    from edit_ui.quickedit_window import QuickEditWindow
elif args.ui_test or args.edit_ui:
    from PyQt5.QtWidgets import QApplication
    from edit_ui.main_window import MainWindow
    from edit_ui.sample_selector import SampleSelector

if args.ui_test:
    print('Testing expanded inpainting UI')
    app = QApplication(sys.argv)
    screen = app.primaryScreen()
    size = screen.availableGeometry()
    def inpaint(selection, mask, prompt, batchSize, batchCount, showSample):
        print("Mock inpainting call:")
        print(f"\tselection: {selection}")
        print(f"\tmask: {mask}")
        print(f"\tprompt: {prompt}")
        print(f"\tbatchSize: {batchSize}")
        print(f"\tbatchCount: {batchCount}")
        print(f"\tshowSample: {showSample}")
        testSample = Image.open(open('mask.png', 'rb')).convert('RGB')
        showSample(testSample, 0, 0)
    d = MainWindow(size.width(), size.height(), None, inpaint)
    d.applyArgs(args)
    d.show()
    app.exec_()
    sys.exit()


device = torch.device('cuda:0' if (torch.cuda.is_available() and not args.cpu) else 'cpu')
print('Using device:', device)

def loadModels(
        model_path="inpainting.pt",
        bert_path="bert.pt",
        kl_path="kl-f8.pt",
        steps=None,
        clip_guidance=False,
        cpu=False,
        ddpm=False,
        ddim=False):
    model_state_dict = torch.load(args.model_path, map_location='cpu')

    model_params = {
        'attention_resolutions': '32,16,8',
        'class_cond': False,
        'diffusion_steps': 1000,
        'rescale_timesteps': True,
        'timestep_respacing': '27',  # Modify this value to decrease the number of
                                     # timesteps.
        'image_size': 32,
        'learn_sigma': False,
        'noise_schedule': 'linear',
        'num_channels': 320,
        'num_heads': 8,
        'num_res_blocks': 2,
        'resblock_updown': False,
        'use_fp16': False,
        'use_scale_shift_norm': False,
        'clip_embed_dim': 768 if 'clip_proj.weight' in model_state_dict else None,
        'image_condition': True if model_state_dict['input_blocks.0.0.weight'].shape[1] == 8 else False,
        'super_res_condition': True if 'external_block.0.0.weight' in model_state_dict else False,
    }

    if ddpm:
        model_params['timestep_respacing'] = 1000
    if ddim:
        if steps:
            model_params['timestep_respacing'] = 'ddim'+str(steps)
        else:
            model_params['timestep_respacing'] = 'ddim50'
    elif steps:
        model_params['timestep_respacing'] = str(steps)

    model_config = model_and_diffusion_defaults()
    model_config.update(model_params)

    if cpu:
        model_config['use_fp16'] = False

    # Load models
    model, diffusion = create_model_and_diffusion(**model_config)
    model.load_state_dict(model_state_dict, strict=False)
    model.requires_grad_(clip_guidance).eval().to(device)

    if model_config['use_fp16']:
        model.convert_to_fp16()
    else:
        model.convert_to_fp32()

    def set_requires_grad(model, value):
        for param in model.parameters():
            param.requires_grad = value

    # vae
    ldm = torch.load(kl_path, map_location="cpu")
    ldm.to(device)
    ldm.eval()
    ldm.requires_grad_(clip_guidance)
    set_requires_grad(ldm, clip_guidance)

    bert = BERTEmbedder(1280, 32)
    sd = torch.load(bert_path, map_location="cpu")
    bert.load_state_dict(sd)

    bert.to(device)
    bert.half().eval()
    set_requires_grad(bert, False)

    # clip
    clip_model, clip_preprocess = clip.load('ViT-L/14', device=device, jit=False)
    clip_model.eval().requires_grad_(False)
    normalize = transforms.Normalize(mean=[0.48145466, 0.4578275, 0.40821073], std=[0.26862954, 0.26130258, 0.27577711])
    return model_params, model, diffusion, ldm, bert, clip_model, clip_preprocess, normalize

modelParams, model, diffusion, ldm, bert, clip_model, clip_preprocess, normalize= loadModels(
        model_path=args.model_path,
        bert_path=args.bert_path,
        kl_path=args.kl_path,
        steps = args.steps,
        clip_guidance = args.clip_guidance,
        cpu = args.cpu,
        ddpm = args.ddpm,
        ddim = args.ddim)
print("Loaded models")

def createSampleFunction(image, mask, prompt, batch_size):
    # bert context
    text_emb = bert.encode([prompt]*batch_size).to(device).float()
    text_blank = bert.encode([args.negative]*batch_size).to(device).float()

    text = clip.tokenize([prompt]*batch_size, truncate=True).to(device)
    text_clip_blank = clip.tokenize([args.negative]*batch_size, truncate=True).to(device)


    # clip context
    text_emb_clip = clip_model.encode_text(text)
    text_emb_clip_blank = clip_model.encode_text(text_clip_blank)

    make_cutouts = MakeCutouts(clip_model.visual.input_resolution, args.cutn)

    text_emb_norm = text_emb_clip[0] / text_emb_clip[0].norm(dim=-1, keepdim=True)

    image_embed = None

    # image context
    if args.edit or args.edit_ui:
        input_image = torch.zeros(1, 4, args.height//8, args.width//8, device=device)
        input_image_pil = None
        np_image = None
        if isinstance(image, Image.Image):
            input_image = torch.zeros(1, 4, args.height//8, args.width//8, device=device)
            input_image_pil = image
        elif args.edit and args.edit.endswith('.npy'):
            with open(args.edit, 'rb') as f:
                np_image = np.load(f)
                np_image = torch.from_numpy(np_image).unsqueeze(0).to(device)
                input_image = torch.zeros(1, 4, args.height//8, args.width//8, device=device)
        elif args.edit:
            w = args.edit_width if args.edit_width else args.width
            h = args.edit_height if args.edit_height else args.height
            input_image_pil = Image.open(fetch(args.edit)).convert('RGB')
            input_image_pil = ImageOps.fit(input_image_pil, (w, h))
        if input_image_pil is not None:
            np_image = transforms.ToTensor()(input_image_pil).unsqueeze(0).to(device)
            np_image = 2 * np_image - 1
            np_image = ldm.encode(np_image).sample()

        y = args.edit_y//8
        x = args.edit_x//8
        ycrop = y + np_image.shape[2] - input_image.shape[2]
        xcrop = x + np_image.shape[3] - input_image.shape[3]

        ycrop = ycrop if ycrop > 0 else 0
        xcrop = xcrop if xcrop > 0 else 0

        input_image[
            0,
            :,
            y if y >=0 else 0:y+np_image.shape[2],
            x if x >=0 else 0:x+np_image.shape[3]
        ] = np_image[
            :,
            :,
            0 if y > 0 else -y:np_image.shape[2]-ycrop,
            0 if x > 0 else -x:np_image.shape[3]-xcrop
        ]
        input_image_pil = ldm.decode(input_image)
        input_image_pil = TF.to_pil_image(input_image_pil.squeeze(0).add(1).div(2).clamp(0, 1))
        input_image *= 0.18215

        if isinstance(mask, Image.Image):
            mask_image = mask.convert('L').point( lambda p: 255 if p < 1 else 0 )
            mask_image.save('mask.png')
            mask_image = mask_image.resize((args.width//8,args.height//8), Image.LANCZOS)
            mask = transforms.ToTensor()(mask_image).unsqueeze(0).to(device)
        elif args.mask:
            mask_image = Image.open(fetch(args.mask)).convert('L')
            mask_image = mask_image.resize((args.width//8,args.height//8), Image.LANCZOS)
            mask = transforms.ToTensor()(mask_image).unsqueeze(0).to(device)
        else:
            from PyQt5.QtWidgets import QApplication
            print('draw the area for inpainting, then close the window')
            app = QApplication(sys.argv)
            d = QuickEditWindow(args.width, args.height, input_image_pil)
            app.exec_()
            mask_image = d.getMask().convert('L').point( lambda p: 255 if p < 1 else 0 )
            mask_image.save('mask.png')
            mask_image = mask_image.resize((args.width//8,args.height//8), Image.ANTIALIAS)
            mask = transforms.ToTensor()(mask_image).unsqueeze(0).to(device)

        mask1 = (mask > 0.5)
        mask1 = mask1.float()

        input_image *= mask1

        image_embed = torch.cat(batch_size*2*[input_image], dim=0).float()
    elif model_params['image_condition']:
        # using inpaint model but no image is provided
        image_embed = torch.zeros(batch_size*2, 4, args.height//8, args.width//8, device=device)

    model_kwargs = {
        "context": torch.cat([text_emb, text_blank], dim=0).float(),
        "clip_embed": torch.cat([text_emb_clip, text_emb_clip_blank], dim=0).float() if model_params['clip_embed_dim'] else None,
        "image_embed": image_embed
    }

    # Create a classifier-free guidance sampling function
    def model_fn(x_t, ts, **kwargs):
        half = x_t[: len(x_t) // 2]
        combined = torch.cat([half, half], dim=0)
        model_out = model(combined, ts, **kwargs)
        eps, rest = model_out[:, :3], model_out[:, 3:]
        cond_eps, uncond_eps = torch.split(eps, len(eps) // 2, dim=0)
        half_eps = uncond_eps + args.guidance_scale * (cond_eps - uncond_eps)
        eps = torch.cat([half_eps, half_eps], dim=0)
        return torch.cat([eps, rest], dim=1)

    cur_t = None

    def cond_fn(x, t, context=None, clip_embed=None, image_embed=None):
        with torch.enable_grad():
            x = x[:batch_size].detach().requires_grad_()

            n = x.shape[0]

            my_t = torch.ones([n], device=device, dtype=torch.long) * cur_t

            kw = {
                'context': context[:batch_size],
                'clip_embed': clip_embed[:batch_size] if model_params['clip_embed_dim'] else None,
                'image_embed': image_embed[:batch_size] if image_embed is not None else None
            }

            out = diffusion.p_mean_variance(model, x, my_t, clip_denoised=False, model_kwargs=kw)

            fac = diffusion.sqrt_one_minus_alphas_cumprod[cur_t]
            x_in = out['pred_xstart'] * fac + x * (1 - fac)

            x_in /= 0.18215

            x_img = ldm.decode(x_in)

            clip_in = normalize(make_cutouts(x_img.add(1).div(2)))
            clip_embeds = clip_model.encode_image(clip_in).float()
            dists = spherical_dist_loss(clip_embeds.unsqueeze(1), text_emb_clip.unsqueeze(0))
            dists = dists.view([args.cutn, n, -1])

            losses = dists.sum(2).mean(0)

            loss = losses.sum() * args.clip_guidance_scale

            return -torch.autograd.grad(loss, x)[0]
 
    if args.ddpm:
        base_sample_fn = diffusion.ddpm_sample_loop_progressive
    elif args.ddim:
        base_sample_fn = diffusion.ddim_sample_loop_progressive
    else:
        base_sample_fn = diffusion.plms_sample_loop_progressive
    def sample_fn(init):
        return base_sample_fn(
            model_fn,
            (batch_size*2, 4, int(args.height/8), int(args.width/8)),
            clip_denoised=False,
            model_kwargs=model_kwargs,
            cond_fn=cond_fn if args.clip_guidance else None,
            device=device,
            progress=True,
            init_image=init,
            skip_timesteps=args.skip_timesteps
        )
    return sample_fn

def generateSamples(sample_fn, save_sample, batch_size, num_batches):
    if args.init_image:
        init = Image.open(args.init_image).convert('RGB')
        init = init.resize((int(args.width),  int(args.height)), Image.LANCZOS)
        init = TF.to_tensor(init).to(device).unsqueeze(0).clamp(0,1)
        h = ldm.encode(init * 2 - 1).sample() *  0.18215
        init = torch.cat(batch_size*2*[h], dim=0)
    else:
        init = None
    for i in range(num_batches):
        cur_t = diffusion.num_timesteps - 1
        samples = sample_fn(init)
        for j, sample in enumerate(samples):
            cur_t -= 1
            if j % 5 == 0 and j != diffusion.num_timesteps - 1:
                save_sample(i, sample)
        save_sample(i, sample, args.clip_score)

def do_run():
    if args.seed >= 0:
        torch.manual_seed(args.seed)
    if args.edit_ui:
        app = QApplication(sys.argv)
        screen = app.primaryScreen()
        size = screen.availableGeometry()
        def inpaint(selection, mask, prompt, batchSize, batchCount, showSample):
            gc.collect()
            sample_fn = createSampleFunction(selection, mask, prompt, batchSize)
            def save_sample(i, sample, clip_score=False):
                for k, image in enumerate(sample['pred_xstart'][:batchSize]):
                    image /= 0.18215
                    im = image.unsqueeze(0)
                    out = ldm.decode(im)
                    out = TF.to_pil_image(out.squeeze(0).add(1).div(2).clamp(0, 1))
                    showSample(out, k, i) 
            generateSamples(sample_fn, save_sample, batchSize, batchCount)
        d = MainWindow(size.width(), size.height(), None, inpaint)
        d.applyArgs(args)
        d.show()
        app.exec_()
        sys.exit()
    else:
        sample_fn = createSampleFunction(None, None, args.text, args.batch_size)
        def save_sample(i, sample, clip_score=False):
            for k, image in enumerate(sample['pred_xstart'][:args.batch_size]):
                image /= 0.18215
                im = image.unsqueeze(0)
                out = ldm.decode(im)

                npy_filename = f'output_npy/{args.prefix}{i * args.batch_size + k:05}.npy'
                with open(npy_filename, 'wb') as outfile:
                    np.save(outfile, image.detach().cpu().numpy())

                out = TF.to_pil_image(out.squeeze(0).add(1).div(2).clamp(0, 1))

                filename = f'output/{args.prefix}{i * args.batch_size + k:05}.png'
                out.save(filename)

                if clip_score:
                    image_emb = clip_model.encode_image(clip_preprocess(out).unsqueeze(0).to(device))
                    image_emb_norm = image_emb / image_emb.norm(dim=-1, keepdim=True)

                    similarity = torch.nn.functional.cosine_similarity(image_emb_norm, text_emb_norm, dim=-1)

                    final_filename = f'output/{args.prefix}_{similarity.item():0.3f}_{i * args.batch_size + k:05}.png'
                    os.rename(filename, final_filename)

                    npy_final = f'output_npy/{args.prefix}_{similarity.item():0.3f}_{i * args.batch_size + k:05}.npy'
                    os.rename(npy_filename, npy_final)
        generateSamples(sample_fn, save_sample, args.batch_size, args.num_batches)


gc.collect()
do_run()
