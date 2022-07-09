import argparse
import sys

# argument parsing:
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
parser.add_argument('--server_test', dest='server_test', action='store_true') # Test backend colab server

# Start inpainting UI in client mode:
parser.add_argument('--inpainting_client', dest='inpainting_client', action='store_true',
                    help='Run inpainting UI only, forwarding image generation tasks to a server')
parser.add_argument('--server_url', type = str, required = False, default = '',
                    help='Server URL, used only when running in client mode')
parser.add_argument('--fast_ngrok_connection', type = str, required = False, default = '',
                    help='If true, connection rates will not be limited when using ngrok. You should only use this if you have a paid ngrok account')

args = parser.parse_args()

if args.edit and not args.mask:
    from edit_ui.quickedit_window import QuickEditWindow
elif args.ui_test or args.edit_ui or args.inpainting_client:
    from PyQt5.QtWidgets import QApplication
    from edit_ui.main_window import MainWindow
    from edit_ui.sample_selector import SampleSelector

if args.inpainting_client:
    from PyQt5 import QtCore
    from PyQt5.QtWidgets import QInputDialog
    import requests
    import base64
    from PIL import Image
    import io
    print('Testing client/server mode:')
    app = QApplication(sys.argv)
    screen = app.primaryScreen()
    size = screen.availableGeometry()
    global window
    def inpaint(selection, mask, prompt, batchSize, batchCount, showSample):
        def imageToBase64(pilImage):
            buffer = io.BytesIO()
            pilImage.save(buffer, format='PNG')
            return str(base64.b64encode(buffer.getvalue()), 'utf-8')
        body = {
            'batch_size': batchSize,
            'num_batches': batchCount,
            'edit': imageToBase64(selection),
            'mask': imageToBase64(mask),
            'prompt': prompt
        }

        def errorCheck(serverResponse, contextStr):
            if serverResponse.status_code != 200:
                if serverResponse.content and ('application/json' in serverResponse.headers['content-type']) \
                        and serverResponse.json() and 'error' in serverResponse.json():
                    raise Exception(f"{serverResponse.status_code} response to {contextStr}: {serverResponse.json()['error']}")
                else:
                    print("RES")
                    print(serverResponse.content)
                    raise Exception(f"{serverResponse.status_code} response to {contextStr}: unknown error")
        res = requests.post(args.server_url, json=body, timeout=5)
        errorCheck(res, 'New inpainting request')
            
        # POST to args.server_url, check response
        # If invalid or error response, throw Exception
        samples = {}
        in_progress = True
        errorCount = 0
        maxErrors = 10
        # refresh times in microseconds:
        minRefresh = 300000
        maxRefresh = 60000000
        if('.ngrok.io' in args.server_url and not args.fast_ngrok_connection):
            # Free ngrok accounts only allow 20 connections per minute, lower the refresh rate to avoid failures:
            minRefresh = 3000000

        while in_progress:
            sleepTime = min(minRefresh * pow(2, errorCount), maxRefresh)
            print(f"Checking for response in {sleepTime//1000} ms...")
            QtCore.QThread.usleep(sleepTime)
            # GET server_url/sample, sending previous samples:
            res = None
            try:
                res = requests.get(f'{args.server_url}/sample', json={'samples': samples}, timeout=5)
                errorCheck(res, 'sample update request')
            except Exception as err:
                errorCount += 1
                print(f'Error {errorCount}: {err}')
                if errorCount > maxErrors:
                    print('Inpainting failed, reached max retries.')
                    break
                else:
                    continue
            errorCount = 0 # Reset error count on success.


            # On valid response, for each entry in res.json.sample:
            jsonBody = res.json()
            if 'samples' not in jsonBody:
                continue
            for sampleName in jsonBody['samples'].keys():
                # GET sample image from server_url/sample/id
                sampleRes = requests.get(f'{args.server_url}/sample/{sampleName}', timeout=5)
                sampleImage = None
                try:
                    errorCheck(sampleRes, f'sample download request for {sampleName}')
                    sampleImage = Image.open(io.BytesIO(sampleRes.content))
                    idx = int(sampleName) % batchSize
                    batch = int(sampleName) // batchSize
                    showSample(sampleImage, idx, batch)
                    samples[sampleName] = jsonBody['samples'][sampleName]
                except Exception as err:
                    print(f'Warning: {err}')
                    errorCount += 1
                    continue
            in_progress = jsonBody['in_progress']

    window = MainWindow(size.width(), size.height(), None, inpaint)
    window.applyArgs(args)
    window.show()

    def promptForURL(promptText):
        newUrl = QInputDialog.getText(window, 'Inpainting UI', promptText)
        if newUrl[1] == False: # User clicked 'Cancel'
            sys.exit()
        if newUrl[0] != '':
            args.server_url=newUrl[0]

    # Get URL if one was not provided on the command line:
    while args.server_url == '':
        promptForURL('Enter server URL:')

    # Check connection:
    def healthCheckPasses():
        try:
            res = requests.get(args.server_url, timeout=1)
            return res.status_code == 200 and ('application/json' in res.headers['content-type']) \
                and 'success' in res.json() and res.json()['success'] == True
        except Exception as err:
            print(f"error connecting to {args.server_url}: {err}")
            return False
    while not healthCheckPasses():
        promptForURL('Server connection failed, enter a new URL or click "OK" to retry')
    app.exec_()
    sys.exit()

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


import gc
import os

from PIL import Image
import torch
from torchvision.transforms import functional as TF
import numpy as np

from startup.utils import *
from startup.load_models import loadModels
from startup.create_sample_function import createSampleFunction
from startup.generate_samples import generateSamples



device = torch.device('cuda:0' if (torch.cuda.is_available() and not args.cpu) else 'cpu')
print('Using device:', device)


model_params, model, diffusion, ldm, bert, clip_model, clip_preprocess, normalize = loadModels(device,
        model_path=args.model_path,
        bert_path=args.bert_path,
        kl_path=args.kl_path,
        steps = args.steps,
        clip_guidance = args.clip_guidance,
        cpu = args.cpu,
        ddpm = args.ddpm,
        ddim = args.ddim)
print("Loaded models")

if args.server_test:
    print('Testing backend server')
    from colab.server import startServer
    app = startServer(device, model_params, model, diffusion, ldm, bert, clip_model, clip_preprocess, normalize)
    app.run(port=5555, host= '0.0.0.0')


def do_run():
    if args.seed >= 0:
        torch.manual_seed(args.seed)
    if args.edit_ui:
        app = QApplication(sys.argv)
        screen = app.primaryScreen()
        size = screen.availableGeometry()
        def inpaint(selection, mask, prompt, batchSize, batchCount, showSample):
            gc.collect()
            sample_fn = createSampleFunction(
                    device,
                    model,
                    model_params,
                    bert,
                    clip_model,
                    ldm,
                    diffusion,
                    normalize,
                    image=None,
                    mask=mask,
                    prompt=prompt,
                    batch_size=batchSize,
                    edit=selection,
                    width=args.width,
                    height=args.height,
                    edit_width=args.edit_width,
                    edit_height=args.edit_height,
                    cutn=args.cutn,
                    clip_guidance=args.clip_guidance,
                    skip_timesteps=args.skip_timesteps,
                    ddpm=args.ddpm,
                    ddim=args.ddim)
            def save_sample(i, sample, clip_score=False):
                for k, image in enumerate(sample['pred_xstart'][:batchSize]):
                    image /= 0.18215
                    im = image.unsqueeze(0)
                    out = ldm.decode(im)
                    out = TF.to_pil_image(out.squeeze(0).add(1).div(2).clamp(0, 1))
                    showSample(out, k, i) 
            generateSamples(ldm, diffusion, sample_fn, save_sample, batchSize, batchCount)
        d = MainWindow(size.width(), size.height(), None, inpaint)
        d.applyArgs(args)
        d.show()
        app.exec_()
        sys.exit()
    else:
        sample_fn = createSampleFunction(
                device,
                model,
                model_params,
                bert,
                clip_model,
                ldm,
                diffusion,
                normalize,
                mask=args.mask,
                prompt=args.text,
                negative=args.negative,
                guidance_scale=args.guidance_scale,
                batch_size=args.batch_size,
                width=args.width,
                height=args.height,
                cutn=args.cutn,
                edit=args.edit,
                edit_width=args.edit_width,
                edit_height=args.edit_height,
                edit_x=args.edit_x,
                edit_y=args.edit_y,
                clip_guidance=args.clip_guidance,
                clip_guidance_scale=args.clip_guidance_scale,
                skip_timesteps=args.skip_timesteps,
                ddpm=args.ddpm,
                ddim=args.ddim)
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
        generateSamples(ldm, diffusion, sample_fn, save_sample, args.batch_size, args.num_batches)

if not args.ui_test and not args.server_test:
    gc.collect()
    do_run()
