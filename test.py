import torch.cuda
import argparse
from SUPIR.util import create_SUPIR_model, PIL2Tensor, Tensor2Numpy, Tensor2PIL, convert_dtype
from PIL import Image
import numpy as np
import os
from torch.nn.functional import interpolate

if torch.cuda.device_count() >= 2:
    SUPIR_device = 'cuda:0'
    LLaVA_device = 'cuda:1'
elif torch.cuda.device_count() == 1:
    SUPIR_device = 'cuda:0'
    LLaVA_device = 'cuda:0'
else:
    raise ValueError('Currently support CUDA only.')

# hyparams here
parser = argparse.ArgumentParser()
parser.add_argument("--img_dir", type=str)
parser.add_argument("--save_dir", type=str)
parser.add_argument("--upscale", type=int, default=1)
parser.add_argument("--SUPIR_sign", type=str, default='Q', choices=['F', 'Q'])
parser.add_argument("--seed", type=int, default=1234)
parser.add_argument("--min_size", type=int, default=1024)
parser.add_argument("--edm_steps", type=int, default=50)
parser.add_argument("--s_stage1", type=int, default=-1)
parser.add_argument("--s_churn", type=int, default=5)
parser.add_argument("--s_noise", type=float, default=1.003)
parser.add_argument("--s_cfg", type=float, default=7.5)
parser.add_argument("--s_stage2", type=float, default=1.)
parser.add_argument("--num_samples", type=int, default=1)
parser.add_argument("--a_prompt", type=str,
                    default='Cinematic, High Contrast, highly detailed, taken using a Canon EOS R '
                            'camera, hyper detailed photo - realistic maximum detail, 32k, Color '
                            'Grading, ultra HD, extreme meticulous detailing, skin pore detailing, '
                            'hyper sharpness, perfect without deformations.')
parser.add_argument("--n_prompt", type=str,
                    default='painting, oil painting, illustration, drawing, art, sketch, oil painting, '
                            'cartoon, CG Style, 3D render, unreal engine, blurring, dirty, messy, '
                            'worst quality, low quality, frames, watermark, signature, jpeg artifacts, '
                            'deformed, lowres, over-smooth')
parser.add_argument("--color_fix_type", type=str, default='Wavelet', choices=["None", "AdaIn", "Wavelet"])
parser.add_argument("--linear_CFG", action='store_true', default=True)
parser.add_argument("--linear_s_stage2", action='store_true', default=False)
parser.add_argument("--spt_linear_CFG", type=float, default=4.0)
parser.add_argument("--spt_linear_s_stage2", type=float, default=0.)
parser.add_argument("--ae_dtype", type=str, default="bf16", choices=['fp32', 'bf16'])
parser.add_argument("--diff_dtype", type=str, default="fp16", choices=['fp32', 'fp16', 'bf16'])
parser.add_argument("--no_llava", action='store_true', default=False)
parser.add_argument("--loading_half_params", action='store_true', default=False)
parser.add_argument("--use_tile_vae", action='store_true', default=False)
parser.add_argument("--encoder_tile_size", type=int, default=512)
parser.add_argument("--decoder_tile_size", type=int, default=64)
parser.add_argument("--load_8bit_llava", action='store_true', default=False)
parser.add_argument("--options", type=str, default='SUPIR_v0', choices=["SUPIR_v0", "SUPIR_v0_Juggernautv9_lightning_tiled", "SUPIR_v0_tiled"])
parser.add_argument("--sampler", type=str, default='DPMPP2M', choices=["EDM", "DPMPP2M"])
parser.add_argument("--use_fast_tile", action='store_true', default=False,
                    help="Use a faster tile encoding/decoding, may impact quality.")
args = parser.parse_args()
print(args)
use_llava = not args.no_llava

# load SUPIR
print('# load SUPIR')
options_file = 'options/' + args.options + '.yaml'
tiled = "TiledRestore" if args.use_tile_vae else "Restore"
sampler_cls = f"sgm.modules.diffusionmodules.sampling.{tiled}{args.sampler}Sampler"
model = create_SUPIR_model(options_file, supir_sign=args.SUPIR_sign, SUPIR_DEVICE, None, sampler_cls).to(SUPIR_device)
print('loaded SUPIR!')
if args.loading_half_params:
    print('# load half model')
    model = model.half()
    print('loaded half model!')
if args.use_tile_vae:
    print('# init tile vae')
    model.init_tile_vae(encoder_tile_size=args.encoder_tile_size, decoder_tile_size=args.decoder_tile_size, use_fast=args.use_fast_tile)
    print('inited tile vae!')
model.ae_dtype = convert_dtype(args.ae_dtype)
model.model.dtype = convert_dtype(args.diff_dtype)
if model is not None:
    print('# load model to ' + SUPIR_device)
    model = model.to(SUPIR_device)
    if getattr(model, 'move_to', None):
        model.move_to(SUPIR_device)
    torch.cuda.set_device(SUPIR_device)
    print('model loaded!')

os.makedirs(args.save_dir, exist_ok=True)
for img_pth in os.listdir(args.img_dir):
    img_name = os.path.splitext(img_pth)[0]
    print('# start process image: ' + img_name)
    captions = []

    LQ_ips = Image.open(os.path.join(args.img_dir, img_pth))
    # Pil -> NumPy
    input_image = np.array(LQ_ips)

    # Pil -> Tensor -> NumPy
    # LQ_img, h0, w0 = PIL2Tensor(LQ_ips, upsacle=args.upscale, min_size=args.min_size)
    # input_image = Tensor2Numpy(LQ_img, h0, w0)
    input_image = HWC3(input_image)
    input_image = upscale_image(input_image, args.upscale, unit_resolution=32,
                                min_size=args.min_size)

    LQ = np.array(input_image) / 255.0
    LQ *= 255.0
    LQ = LQ.round().clip(0, 255).astype(np.uint8)
    LQ = LQ / 255 * 2 - 1
    LQ = torch.tensor(LQ, dtype=torch.float32).permute(2, 0, 1).unsqueeze(0).to(SUPIR_device)[:, :3, :, :]

    # # step 3: Diffusion Process
    print('# start batchify sample')
    samples = model.batchify_sample(LQ, captions, num_steps=args.edm_steps, restoration_scale=args.s_stage1, s_churn=args.s_churn,
                                    s_noise=args.s_noise, cfg_scale=args.s_cfg, control_scale=args.s_stage2, seed=args.seed,
                                    num_samples=args.num_samples, p_p=args.a_prompt, n_p=args.n_prompt, color_fix_type=args.color_fix_type,
                                    use_linear_CFG=args.linear_CFG, use_linear_control_scale=args.linear_s_stage2,
                                    cfg_scale_start=args.spt_linear_CFG, control_scale_start=args.spt_linear_s_stage2)
    print('batchified!')
    # save
    for _i, sample in enumerate(samples):
        Tensor2PIL(sample, h0, w0).save(f'{args.save_dir}/{img_name}_{_i}.png')

