import torch
import argparse
import os
import numpy as np
import datetime
import time
from tqdm import tqdm
import cv2
from torch.utils.data import DataLoader
from torch import optim
from tensorboardX import SummaryWriter
from PIL import Image
from random import sample
import pytz
from deepvoxels.projection import ProjectionHelper
from deepvoxels.dataio import TestDataset
from deepvoxels.deep_voxels import DeepVoxels
from deepvoxels.util import parse_intrinsics, custom_load
from deepvoxels import data_util
from deepvoxel_style_transfer import StyleTransferModel
import utils

# Parse arguments
parser = argparse.ArgumentParser()
parser.add_argument("--data_root", type=str, required=True)
parser.add_argument("--logging_root", type=str, required=True)
parser.add_argument("--checkpoint", required=True)
parser.add_argument("--style_image_path", type=str, required=True)
parser.add_argument("--num_iterations", type=int, required=True)
parser.add_argument("--img_sidelength", type=int, default=512)
parser.add_argument("--no_occlusion_net", action="store_true", default=False)
parser.add_argument("--style_coeff", default=0.5, type=float)
parser.add_argument("--content_coeff", default=0.5, type=float)
opt = parser.parse_args()
print("\n".join([f"({key}, {value})" for key, value in vars(opt).items()]))

# Some constants
device = data_util.get_device()
proj_image_dims = [64, 64]
grid_dim = 32
grid_dims = 3*[grid_dim]
num_grid_feats = 64
nf0 = 64
input_image_dims = [opt.img_sidelength, opt.img_sidelength]
_, grid_barycenter, scale, near_plane, _ = parse_intrinsics(
    os.path.join(opt.data_root, "intrinsics.txt"),
    trgt_sidelength=input_image_dims[0]
)
if near_plane == 0.0:
    near_plane = np.sqrt(3)/2
voxel_size = (1. / grid_dim) * scale
grid_origin = torch.tensor(np.eye(4)).float().to(device).squeeze()
grid_origin[:3, 3] = grid_barycenter

lift_intrinsic = parse_intrinsics(os.path.join(opt.data_root, "intrinsics.txt"), trgt_sidelength=proj_image_dims[0])[0]
proj_intrinsic = lift_intrinsic

depth_min = 0.
depth_max = grid_dim * voxel_size + near_plane
frustrum_depth = 2 * grid_dims[-1]

batch_size = 3

# Result logging directory
tz = pytz.timezone("US/Eastern")
d = datetime.datetime.now(tz)
dir_name = os.path.join(
    d.strftime('%m_%d'),
    d.strftime('%H-%M-%S_') + '_'.join(opt.checkpoint.strip('/').split('/')[-2:]) + '_' + opt.data_root.strip('/').split('/')[-1]
)
traj_dir = os.path.join(opt.logging_root, 'test_traj', dir_name)
depth_dir = os.path.join(traj_dir, 'depth')
runs_dir = os.path.join(opt.logging_root, "runs", dir_name)
data_util.cond_mkdir(traj_dir)
data_util.cond_mkdir(depth_dir)
data_util.cond_mkdir(runs_dir)

# Define DeepVoxel Model
model = DeepVoxels(
    lifting_img_dims = proj_image_dims,
    frustrum_img_dims = proj_image_dims,
    grid_dims = grid_dims,
    use_occlusion_net=not opt.no_occlusion_net,
    num_grid_feats = num_grid_feats,
    nf0 = nf0,
    img_sidelength = input_image_dims[0],
)
custom_load(model, opt.checkpoint)
for param in model.parameters():
    param.requires_grad = False
model.eval()
model.to(device)

# Project Module
projection = ProjectionHelper(
    projection_intrinsic = proj_intrinsic,
    lifting_intrinsic = lift_intrinsic,
    depth_min = depth_min,
    depth_max = depth_max,
    projection_image_dims = proj_image_dims,
    lifting_image_dims = proj_image_dims,
    grid_dims = grid_dims,
    voxel_size = voxel_size,
    device = device,
    frustrum_depth = frustrum_depth,
    near_plane = near_plane,
)

# Style Transfer Model
stm = StyleTransferModel()
# Generating an image from trained checkpoint and projection file

dataset = TestDataset(pose_dir=os.path.join(opt.data_root, 'pose'))
dataloader = DataLoader(dataset, batch_size=batch_size, shuffle=True, num_workers=4)

dv = model.deepvoxels.detach().clone()
dv.requires_grad = True

dv_orig = model.deepvoxels.detach().clone()

optimizer = optim.Adam([dv])

forward_time = 0.
print('Starting generation of images...')
iter = 0
depth_imgs = []

writer = SummaryWriter(runs_dir, flush_secs=20)

# Add style image to writer
style_img = utils.load_rgb(opt.style_image_path, (opt.img_sidelength, opt.img_sidelength))
writer.add_image("Input images src style", style_img+0.5, 0)
style_img = np.expand_dims(style_img, axis=0)
style_img = torch.tensor(style_img, device=device, dtype=torch.float)

# Utility function
def get_images_from_poses(trgt_poses, dv):
    # Add some initial renderings of the network to writer
    trgt_poses = trgt_poses.to(device)
    proj_ind_3d = []
    proj_ind_2d = []
    for i in range(trgt_poses.shape[0]):
        proj_mapping = projection.compute_proj_idcs(trgt_poses[i], grid_origin)
        if proj_mapping is None:
            print("Invalid sample")
            continue
        proj_ind_3d.append(proj_mapping[0])
        proj_ind_2d.append(proj_mapping[1])
        
    output, _ = model(
        None,
        proj_ind_3d,
        proj_ind_2d,
        None, None, None, dv
    )
    output = torch.cat(output)
    return output

# Utility Function
def concate_images(images):
    concatenated_images = torch.empty((3, opt.img_sidelength, batch_size*opt.img_sidelength))
    for i in range(images.shape[0]):
        concatenated_images[:, :, i*opt.img_sidelength:(i+1)*opt.img_sidelength] = images[i]
    return concatenated_images
    
with torch.no_grad():
    trgt_poses_indices = sample(range(len(dataset)), batch_size)
    trgt_poses = torch.cat([dataset[i].unsqueeze(dim=0) for i in trgt_poses_indices])
    output_images = get_images_from_poses(trgt_poses, dv_orig)
    concatenated_images = concate_images(output_images)
    writer.add_image("Initial-Rendered-Images", concatenated_images + 0.5, 0)

# Train
style_features_dst = stm.extract_style_features(style_img)
for epoch in range(opt.num_iterations):
    print(f"Epoch: {epoch}")
    for batch_num, trgt_poses in enumerate(tqdm(dataloader)):
        optimizer.zero_grad()
        
        orig_images = get_images_from_poses(trgt_poses, dv_orig)
        content_features_dst = stm.extract_content_features(orig_images)

        output_images = get_images_from_poses(trgt_poses, dv)
        content_features_src = stm.extract_content_features(output_images)
        style_features_src = stm.extract_style_features(output_images)

        style_loss = stm.get_style_loss(style_features_src, style_features_dst)
        content_loss = stm.get_content_loss(content_features_src, content_features_dst)
        loss = (opt.style_coeff) * style_loss + (opt.content_coeff) * content_loss

        loss.backward()
        optimizer.step()
        
        batch_num += len(dataloader) * epoch
        writer.add_scalar("Overall-DV-SSE", torch.sum((dv.detach() - model.deepvoxels.detach())**2), batch_num)
        writer.add_scalars("Loss", {
            "style loss(scaled)": style_loss.item() * opt.style_coeff, 
            "content loss(scaled)": content_loss.item() * opt.content_coeff,
            "ovearll loss": loss.item()
        }, batch_num)
        concatenated_images = concate_images(output_images)
        writer.add_image("Rendered-Images", concatenated_images + 0.5, batch_num)
writer.close()