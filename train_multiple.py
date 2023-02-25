import monai
from monai.data import ImageDataset, create_test_image_3d, decollate_batch, DataLoader, PatchIter, GridPatchDataset
from monai.inferers import sliding_window_inference
from monai.metrics import DiceMetric
from monai.transforms import Activations, EnsureChannelFirst, AsDiscrete, Compose, RandRotate90, RandSpatialCrop, ScaleIntensity, CenterSpatialCrop
from monai.visualize import plot_2d_or_3d_image
from monai.utils import set_determinism
import monai.losses as lo
from monai.losses.dice import DiceLoss


import torch
from torch.utils.tensorboard import SummaryWriter

import logging
import os
import sys
import tempfile
from glob import glob

import nibabel as nib
import numpy as np
import random

import sys
from UNetv2 import UNetv2
from monai.networks.nets.unet import UNet
# from orig_UNet_but_with_avg_pool_res_unit import UNet_avg_pool
from HEMISv2 import HEMISv2
from create_modality import create_modality
from multi_scale_fusion_net import MSFN

def create_dataloader(val_size: int, images, segs, workers, train_batch_size: int, total_train_data_size: int, current_train_data_size: int, cropped_input_size:list):

    train_imtrans = Compose(
        [
            EnsureChannelFirst(strict_check=True),
            RandSpatialCrop((cropped_input_size[0], cropped_input_size[1], cropped_input_size[2]), random_size=False),
            RandRotate90(prob=0.1, spatial_axes=(0, 2)),
        ]
    )
    train_segtrans = Compose(
        [
            EnsureChannelFirst(strict_check=True),
            RandSpatialCrop((cropped_input_size[0], cropped_input_size[1], cropped_input_size[2]), random_size=False),
            RandRotate90(prob=0.1, spatial_axes=(0, 2)),
        ]
    )
    val_imtrans = Compose([EnsureChannelFirst()])
    val_segtrans = Compose([EnsureChannelFirst()])

    div = total_train_data_size//current_train_data_size
    rem = total_train_data_size%current_train_data_size

    train_images = images[:-val_size]
    train_images = train_images * div + train_images[:rem]
    train_segs = segs[:-val_size]
    train_segs = train_segs * div + train_segs[:rem]

    # create a training data loader
    train_ds = ImageDataset(train_images, train_segs, transform=train_imtrans, seg_transform=train_segtrans)
    train_loader = DataLoader(train_ds, batch_size=train_batch_size, shuffle=True, num_workers=workers, pin_memory=0)
    # train_loader = DataLoader(train_ds, batch_size=4, shuffle=True, num_workers=workers)
    # create a validation data loader
    val_ds = ImageDataset(images[-val_size:], segs[-val_size:], transform=val_imtrans, seg_transform=val_segtrans)
    val_loader = DataLoader(val_ds, batch_size=1, num_workers=workers, pin_memory=0)

    return train_loader, val_loader

def rand_set_channels_to_zero(dataset_modalities: list, batch_img_data: torch.Tensor):
    number_of_dropped_modalities = np.random.randint(0,len(dataset_modalities))
    modalities_dropped = random.sample(list(np.arange(len(dataset_modalities))), number_of_dropped_modalities)
    modalities_dropped.sort()
    # modalities_remaining = list(set(modalities_array) - set(modalities_dropped))
    batch_img_data[:,modalities_dropped,:,:,:] = 0.
    modalities_remaining = list(set(np.arange(len(dataset_modalities))) - set(modalities_dropped))
    
    return modalities_remaining, batch_img_data

def remove_random_channels(dataset_modalities: list, batch_img_data: torch.Tensor):
    number_of_dropped_modalities = np.random.randint(0,len(dataset_modalities))
    modalities_dropped = random.sample(list(np.arange(len(dataset_modalities))), number_of_dropped_modalities)
    modalities_remaining = list(set(np.arange(len(dataset_modalities))) - set(modalities_dropped))
    inputs = batch_img_data[:,modalities_remaining,:,:,:]

    return modalities_remaining, inputs

def augment(batch_img_data:torch.Tensor):
    modalities_to_merge = random.sample(list(np.arange(batch_img_data.shape[1])),2)
    mod_1 = batch_img_data[:,[modalities_to_merge[0]]]
    mod_2 = batch_img_data[:,[modalities_to_merge[1]]]
    new_modality = create_modality(mod_1, mod_2)
    batch_img_data = torch.cat((batch_img_data, torch.from_numpy(new_modality)), dim=1)

    return batch_img_data
    

if __name__ == "__main__":
    monai.config.print_config()
    logging.basicConfig(stream=sys.stdout, level=logging.INFO)
    print("original_setup.py")
    # images are 240x240x155

    print("USING BRAINMASK NORMED")

    # dataset = "BRATS"

    set_determinism(seed=0)
    print("determinism seed = 0")

    device_id = int(sys.argv[1])
    epochs = int(sys.argv[2])
    save_name = str(sys.argv[3])
    dataset = str(sys.argv[4])
    randomly_drop = bool(int(sys.argv[5]))
    BRATS_two_channel_seg = bool(int(sys.argv[6]))
    lr_lower_lim = float(sys.argv[7])
    model_type = str(sys.argv[8])
    augment_modalities = bool(int(sys.argv[9]))

    # device_id = 0
    # epochs = 1000
    # save_name = "DEL"
    # # modalities = "0123"
    # dataset = "BRATS"
    # randomly_drop = 0
    # BRATS_two_channel_seg = 0
    # lr_lower_lim = 1e-5
    # model_type = "MSFN"
    # augment_modalities = False

    print("lr lower lim: ", lr_lower_lim)

    workers = 2
    train_batch_size = 1
    cropped_input_size = [128,128,128]
    val_interval = 4
    lr = 1e-3
    print("lr: ",lr)

    print("Workers: ", workers)
    print("Batch size: ", train_batch_size)
    print("2 channel seg: ", int(BRATS_two_channel_seg))
    print("Augment modalities?: ", augment_modalities)

    total_size_BRATS = 484
    total_size_ATLAS = 654
    total_size_MSSEG = 53
    total_size_ISLES = 28

    train_size_BRATS = 444
    train_size_ATLAS = 459
    train_size_MSSEG = 37
    train_size_ISLES = 20

    channels_BRATS = ["FLAIR", "T1", "T1c", "T2"]
    channels_ATLAS = ["T1"]
    channels_MSSEG = ["FLAIR","T1","T1c","T2","DP"]
    channels_ISLES = ["FLAIR", "T1", "T2", "DWI"]

    data_size = max(("BRATS" in dataset) * train_size_BRATS, ("ATLAS" in dataset) * train_size_ATLAS, ("MSSEG" in dataset) * train_size_MSSEG, ("ISLES" in dataset) * train_size_ISLES)
    
    total_modalities = set(("BRATS" in dataset) * channels_BRATS).union(set(("ATLAS" in dataset) * channels_ATLAS)).union(set(("MSSEG" in dataset) * channels_MSSEG)).union(set(("ISLES" in dataset) * channels_ISLES))
    total_modalities = sorted(list(total_modalities))
    

    def map_channels(dataset_channels, total_modalities):
        channel_map = []
        for channel in dataset_channels:
            for index, modality in enumerate(total_modalities):
                if channel == modality:
                    channel_map.append(index)
        return channel_map

    BRATS_channel_map = map_channels(channels_BRATS, total_modalities)
    ATLAS_channel_map = map_channels(channels_ATLAS, total_modalities)
    MSSEG_channel_map = map_channels(channels_MSSEG, total_modalities)
    ISLES_channel_map = map_channels(channels_ISLES, total_modalities)

    # ////////CHANGE THIS /////////////
    # /////////////////////////////////////////
    # /////////////////////////////////////////
    # /////////////////////////////////////////
    # /////////////////////////////////////////
    # /////////////////////////////////////////
    # /////////////////////////////////////////
    # /////////////////////////////////////////
    # /////////////////////////////////////////
    # /////////////////////////////////////////
    # /////////////////////////////////////////
    # /////////////////////////////////////////
    # /////////////////////////////////////////
    # /////////////////////////////////////////
    # /////////////////////////////////////////
    # /////////////////////////////////////////
    # print("MANUALLY SETTING ISLES CHANNEL MAP")
    # ISLES_channel_map = [1,2,4,3]
    # total_modalities = ['DP', 'FLAIR', 'T1', 'T1c', 'T2']
    
    print("Total modalities: ", total_modalities)
    print("BRATS channel map: ", BRATS_channel_map)
    print("ATLAS channel map: ", ATLAS_channel_map)
    print("MSSEG channel map: ", MSSEG_channel_map)
    print("ISLES channel map: ", ISLES_channel_map)

    
    train_loaders = []
    val_loaders = []
    data_loader_map = {}

    if "BRATS" in dataset:
        print("Training BRATS")
        val_size = total_size_BRATS-train_size_BRATS

        img_path = "/home/shared_space/data/BRATS_Decathlon_2016_17/BRATS_Normalised_with_brainmask/normed"
        if BRATS_two_channel_seg:
            seg_path = "/home/shared_space/data/BRATS_Decathlon_2016_17/two_channel_labels"
        else:
            seg_path = "/home/shared_space/data/BRATS_Decathlon_2016_17/BRATS_merged_labels_inc_edema"

        print(seg_path)

        images = sorted(glob(os.path.join(img_path, "BRATS*_normed_on_mask.nii.gz")))
        segs = sorted(glob(os.path.join(seg_path, "BRATS*merged.nii.gz")))

        train_loader_BRATS, val_loader_BRATS = create_dataloader(val_size=val_size, images=images,segs=segs, workers=workers,train_batch_size=train_batch_size,total_train_data_size=data_size,current_train_data_size=train_size_BRATS,cropped_input_size=cropped_input_size)
        
        data_loader_map["BRATS"] = len(train_loaders)
        train_loaders.append(train_loader_BRATS)
        val_loaders.append(val_loader_BRATS)

    if "ATLAS" in dataset:
        print("Training ATLAS")
        val_size = total_size_ATLAS-train_size_ATLAS

        img_path_ATLAS = "/home/sedm6251/projectMaterial/skullStripped/ATLAS/ATLAS/normed_images"
        seg_path_ATLAS = "/home/sedm6251/projectMaterial/skullStripped/ATLAS/ATLAS/trimmed_labels_ints"
        images = sorted(glob(os.path.join(img_path_ATLAS, "*_normed.nii.gz")))
        segs = sorted(glob(os.path.join(seg_path_ATLAS, "*_label_trimmed.nii.gz")))

        train_loader_ATLAS, val_loader_ATLAS = create_dataloader(val_size=val_size, images=images,segs=segs, workers=workers,train_batch_size=train_batch_size,total_train_data_size=data_size,current_train_data_size=train_size_ATLAS,cropped_input_size=cropped_input_size)

        data_loader_map["ATLAS"] = len(train_loaders)
        train_loaders.append(train_loader_ATLAS)
        val_loaders.append(val_loader_ATLAS)

    if "MSSEG" in dataset:
        print("Training MSSEG")
        val_size = total_size_MSSEG-train_size_MSSEG

        img_path = "/home/sedm6251/projectMaterial/datasets/MSSEG_2016/Normed"
        seg_path = "/home/sedm6251/projectMaterial/datasets/MSSEG_2016/Labels"
        images = sorted(glob(os.path.join(img_path, "*.nii.gz")))
        segs = sorted(glob(os.path.join(seg_path, "*.nii.gz")))

        train_loader_MSSEG, val_loader_MSSEG = create_dataloader(val_size=val_size, images=images,segs=segs, workers=workers,train_batch_size=train_batch_size,total_train_data_size=data_size,current_train_data_size=train_size_MSSEG,cropped_input_size=cropped_input_size)

        data_loader_map["MSSEG"] = len(train_loaders)
        train_loaders.append(train_loader_MSSEG)
        val_loaders.append(val_loader_MSSEG)

    if "ISLES" in dataset:
        print("Training ISLES")
        val_size = total_size_ISLES-train_size_ISLES

        img_path = "/home/sedm6251/projectMaterial/baseline_models/ISLES2015/Data/images"
        seg_path = "/home/sedm6251/projectMaterial/baseline_models/ISLES2015/Data/labels"
        images = sorted(glob(os.path.join(img_path, "*.nii.gz")))
        segs = sorted(glob(os.path.join(seg_path, "*.nii.gz")))

        train_loader_ISLES, val_loader_ISLES = create_dataloader(val_size=val_size, images=images,segs=segs, workers=workers,train_batch_size=train_batch_size,total_train_data_size=data_size,current_train_data_size=train_size_ISLES,cropped_input_size=cropped_input_size)

        data_loader_map["ISLES"] = len(train_loaders)
        train_loaders.append(train_loader_ISLES)
        val_loaders.append(val_loader_ISLES)

    print("Running on GPU:" + str(device_id))
    print("Running for epochs:" + str(epochs))
    # print("Final layer strides:" + str(2))
    # print("modalities " + modalities)

    # modalities_array = list(modalities)
    # for index, mode in enumerate(modalities_array):
    #     int_mode = int(mode)
    #     modalities_array[index] = int_mode
    # print(modalities_array)

    cuda_id = "cuda:" + str(device_id)
    device = torch.device(cuda_id)
    torch.cuda.set_device(cuda_id)

    dice_metric = DiceMetric(include_background=True, reduction="mean", get_not_nans=False)
    post_trans = Compose([Activations(sigmoid=True), AsDiscrete(threshold=0.5)])

    # create UNet, DiceLoss and Adam optimizer
    print("Layers: 16,32,64,128,256")
    print("Dropout: 0.2")

    if model_type == "UNET":
        print("TRAINING WITH UNET")
        model = UNetv2(
            spatial_dims=3,
            in_channels=len(total_modalities),
            out_channels=1,
            kernel_size = (3,3,3),
            channels=(16, 32, 64, 128, 256),
            strides=((2,2,2),(2,2,2),(2,2,2),(2,2,2)),
            num_res_units=2,
            dropout=0.2,
        ).to(device)

        #//////////////////////////////////////////////////////
        #//////////////////////////////////////////////////////
        #//////////////////////////////////////////////////////
        # load_model_path = "/home/sedm6251/projectMaterial/baseline_models/Combined_Training/TRAIN_BRATS_ATLAS_MSSEG/UNET/RAND/UNET_BRATS_ATLAS_MSSEG_RAND_BEST_ATLAS.pth"
        # print("LOADING MODEL: ", load_model_path)
        # model.load_state_dict(torch.load(load_model_path, map_location={"cuda:0":cuda_id,"cuda:1":cuda_id}))

    elif model_type == "HEMIS_spatial_attention":
        print("TRAINING WITH HEMIS SPATIAL ATTENTION")
        model = HEMISv2(
            post_seg_res_units=False,
            fusion_type="spatial attention",
            UNet_outs=12,
            conv1_in=12,
            conv1_out=12,
            conv2_in=12,
            conv2_out=12,
            conv3_in=12,
            pred_uncertainty=False,
            grid_UNet=True,
        ).to(device)

        #///////////////////////////////////////////////////
        #///////////////////////////////////////////////////
        # load_model_path = "/home/sedm6251/projectMaterial/baseline_models/Combined_Training/TRAIN_BRATS_ATLAS_ISLES/HEM_SPATIAL_ATTENTION/HEM_Spatial_Attention_BRATS_ATLAS_ISLES_RAND_BEST_ATLAS.pth"
        # print("LOADING MODEL: ", load_model_path)
        # model.load_state_dict(torch.load(load_model_path, map_location={"cuda:0":cuda_id,"cuda:1":cuda_id}))
    elif model_type == "MSFN":
        model = MSFN(paired=True).to(device)
        # load_model_path = "/home/sedm6251/projectMaterial/baseline_models/Combined_Training/TRAIN_BRATS_ATLAS_ISLES/MSFN/MSFN_BRATS_ATLAS_ISLES_RAND_BEST_ATLAS.pth"
        # print("LOADING MODEL: ", load_model_path)
        # model.load_state_dict(torch.load(load_model_path, map_location={"cuda:0":cuda_id,"cuda:1":cuda_id}))


    # model_path = "/home/sedm6251/projectMaterial/baseline_models/BRATS_Decathlon_2016_17/Single_Unet_modality_1_T1w_brain_mask_normed.pth"
    # model.load_state_dict(torch.load(model_path, map_location={"cuda:0":cuda_id,"cuda:1":cuda_id}))
    loss_function = DiceLoss(sigmoid=True)
    # loss_function = monai.losses.DiceLoss(sigmoid=True)
    
    
    optimizer = torch.optim.Adam(model.parameters(), lr=lr)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer=optimizer, mode="max", factor=0.2, patience=10, verbose=True,min_lr=lr_lower_lim)

    print("in_channels=",len(total_modalities))
    print("batch size = ",train_batch_size)

    # start a typical PyTorch training
    
    best_metric_BRATS = -1
    best_metric_ATLAS = -1
    best_metric_MSSEG = -1
    best_metric_ISLES = -1


    best_metric_epoch_BRATS = -1
    best_metric_epoch_ATLAS = -1
    best_metric_epoch_MSSEG = -1
    best_metric_epoch_ISLES = -1
    # epoch_loss_values = list()
    metric_values = list()
    # if dataset == "BRATS":
    #     log_save = "/home/sedm6251/projectMaterial/baseline_models/BRATS_Decathlon_2016_17/runs/" + save_name
    # elif dataset == "ATLAS":
    #     log_save = "/home/sedm6251/projectMaterial/baseline_models/ATLAS/runs/" + save_name
    # elif dataset == "MSSEG":
    #     log_save = "/home/sedm6251/projectMaterial/baseline_models/MSSEG/runs/" + save_name
    # elif dataset == "ISLES":
    #     log_save = "/home/sedm6251/projectMaterial/baseline_models/ISLES2015/runs/" + save_name
    log_save = "/home/sedm6251/projectMaterial/baseline_models/Combined_Training/runs/" + save_name
    model_save_path = "/home/sedm6251/projectMaterial/baseline_models/Combined_Training/"

    writer = SummaryWriter(log_dir=log_save)
    for epoch in range(epochs):
        print("-" * 10)
        print(f"epoch {epoch + 1}/{epochs}")
        model.train()
        epoch_loss = 0
        step = 0

        if epoch == 150:
            for g in optimizer.param_groups:
                g['lr'] = 1e-4

        for batch_data in zip(*train_loaders):
 
            step += 1

            # # if FLAIR and T2 missing, do not keep edema in labels
            # if (0 not in modalities_array) and (3 not in modalities_array):
            #     seg_channel = 1
            # else:
            #     seg_channel = 0
            outputs = []
            labels = []
            if "BRATS" in dataset:
                loader_index = data_loader_map["BRATS"]
                batch = batch_data[loader_index]

                if model_type == "UNET":

                    if randomly_drop:
                        modalities_remaining, batch[0] = rand_set_channels_to_zero(channels_BRATS, batch[0])

                        # this part is only relevant for BRATS when doing 2 channel segmentation
                        if (0 not in modalities_remaining) and (3 not in modalities_remaining):
                            # Edema cannot be seen so change segmentation to labels without edema
                            seg_channel = 1
                        else:
                            seg_channel = 0
                        if BRATS_two_channel_seg:
                            label = batch[1][:,[seg_channel],:,:,:].to(device)
                        else:
                            label = batch[1].to(device)
                    else:
                        label = batch[1].to(device)
                    
                    input_data = torch.from_numpy(np.zeros((batch[0].shape[0],len(total_modalities),cropped_input_size[0],cropped_input_size[1],cropped_input_size[2]),dtype=np.float32))
                    input_data[:,BRATS_channel_map,:,:,:] = batch[0]
                    input_data = input_data.to(device)
                else:
                    if randomly_drop:
                        modalities_remaining, batch[0] = remove_random_channels(channels_BRATS, batch[0])

                        # this part is only relevant for BRATS when doing 2 channel segmentation
                        if (0 not in modalities_remaining) and (3 not in modalities_remaining):
                            # Edema cannot be seen so change segmentation to labels without edema
                            seg_channel = 1
                        else:
                            seg_channel = 0
                        if BRATS_two_channel_seg:
                            label = batch[1][:,[seg_channel],:,:,:].to(device)
                        else:
                            label = batch[1].to(device)
                    else:
                        label = batch[1].to(device)
                    
                    if augment_modalities:
                        if batch[0].shape[1] > 1:
                            should_create_modality = bool(random.randint(0,1)) 
                            if should_create_modality:
                                batch[0] = augment(batch[0])

                    input_data = batch[0].to(device)
                
                out = model(input_data)
                outputs.append(out)
                labels.append(label)
                

            if "ATLAS" in dataset:
                loader_index = data_loader_map["ATLAS"]
                batch = batch_data[loader_index]

                if model_type == "UNET":
                    input_data = torch.from_numpy(np.zeros((batch[0].shape[0],len(total_modalities),cropped_input_size[0],cropped_input_size[1],cropped_input_size[2]),dtype=np.float32))
                    input_data[:,ATLAS_channel_map,:,:,:] = batch[0]
                    input_data = input_data.to(device)
                else:
                    input_data = batch[0].to(device)
                label = batch[1].to(device)
                out = model(input_data)
                outputs.append(out)
                labels.append(label)

            if "MSSEG" in dataset:
                loader_index = data_loader_map["MSSEG"]
                batch = batch_data[loader_index]

                if model_type == "UNET":
                    if randomly_drop:
                        _, batch[0] = rand_set_channels_to_zero(channels_MSSEG, batch[0])

                    input_data = torch.from_numpy(np.zeros((batch[0].shape[0],len(total_modalities),cropped_input_size[0],cropped_input_size[1],cropped_input_size[2]),dtype=np.float32))
                    input_data[:,MSSEG_channel_map,:,:,:] = batch[0]
                    input_data = input_data.to(device)
                else:
                    if randomly_drop:
                        _, batch[0] = remove_random_channels(channels_MSSEG, batch[0])

                    if augment_modalities:
                        if batch[0].shape[1] > 1:
                            should_create_modality = bool(random.randint(0,1))
                            if should_create_modality:
                                batch[0] = augment(batch[0])
                        

                    input_data = batch[0].to(device)
                label = batch[1].to(device)
                out = model(input_data)
                outputs.append(out)
                labels.append(label)
                # del input_data
                # del label
                # del out
                # del batch
                # del batch_data
                # torch.cuda.empty_cache()

            if "ISLES" in dataset:
                loader_index = data_loader_map["ISLES"]
                batch = batch_data[loader_index]

                if model_type == "UNET":
                    if randomly_drop:
                        _, batch[0] = rand_set_channels_to_zero(channels_ISLES, batch[0])                    
                    input_data = torch.from_numpy(np.zeros((batch[0].shape[0],len(total_modalities),cropped_input_size[0],cropped_input_size[1],cropped_input_size[2]),dtype=np.float32))
                    input_data[:,ISLES_channel_map,:,:,:] = batch[0]
                    input_data = input_data.to(device)
                else:
                    if randomly_drop:
                        _, batch[0] = remove_random_channels(channels_ISLES, batch[0])
                    if augment_modalities:
                        if batch[0].shape[1] > 1:
                            should_create_modality = bool(random.randint(0,1))
                            if should_create_modality:
                                batch[0] = augment(batch[0])
                    input_data = batch[0].to(device)
                label = batch[1].to(device)
                out = model(input_data)
                outputs.append(out)
                labels.append(label)

            # print(inputs.shape)
            # print(batch_data[0][:,[0,2],:,:].shape)
            
            optimizer.zero_grad()

            combined_outs = torch.cat(outputs, dim=0)
            combined_labels = torch.cat(labels,dim=0)

            loss = loss_function(combined_outs, combined_labels)
            loss.backward()
            optimizer.step()
            epoch_loss += loss.item()
            epoch_len = data_size  // train_batch_size
            print(f"{step}/{epoch_len}, train_loss: {loss.item():.4f}")
            writer.add_scalar("train_loss", loss.item(), epoch_len * epoch + step)
        epoch_loss /= step
        # epoch_loss_values.append(epoch_loss)
        print(f"epoch {epoch + 1} average loss: {epoch_loss:.4f}")

        if (epoch+1) % 50 == 0:
            
            model_save_name = model_save_path + save_name + "_Epoch_" + str(epoch) + ".pth"
            torch.save(model.state_dict(), model_save_name)
            print("Saved Model")

        if (epoch + 1) % val_interval == 0:
            model.eval()
            with torch.no_grad():
                seg_channel = 0
                val_images = None
                val_labels = None
                val_outputs = None

                dice_metric.reset()

                if "BRATS" in dataset:

                    loader_index = data_loader_map["BRATS"]
                    for val_data in val_loader_BRATS:
                        # batch = val_data[loader_index]
                        if model_type == "UNET":
                            input_data = torch.from_numpy(np.zeros((1,len(total_modalities),val_data[0].shape[2],val_data[0].shape[3],val_data[0].shape[4]),dtype=np.float32))
                            input_data[:,BRATS_channel_map,:,:,:] = val_data[0]
                        else:
                            input_data = val_data[0]
                        input_data = input_data.to(device)

                        if BRATS_two_channel_seg:
                            label = val_data[1][:,[0],:,:,:].to(device)
                        else:
                            label = val_data[1].to(device)
                        
                        roi_size = (cropped_input_size[0], cropped_input_size[1], cropped_input_size[2])
                        sw_batch_size = 1
                        val_outputs = sliding_window_inference(input_data, roi_size, sw_batch_size, model)
                        val_outputs = [post_trans(i) for i in decollate_batch(val_outputs)]
                        # compute metric for current iteration
                        dice_metric(y_pred=val_outputs, y=label)
                    metric_BRATS = dice_metric.aggregate().item()
                    # reset the status for next validation round
                    dice_metric.reset()

                    if metric_BRATS > best_metric_BRATS:
                        best_metric_BRATS = metric_BRATS
                        best_metric_epoch_BRATS = epoch + 1
                        if epoch>150:
                            model_save_name = model_save_path + save_name + "_BEST_BRATS.pth"
                            torch.save(model.state_dict(), model_save_name)
                            print("saved new best metric model for BRATS")
                    print(
                        "current epoch: {} current mean dice BRATS: {:.4f} best mean dice BRATS: {:.4f} at epoch {}".format(
                            epoch + 1, metric_BRATS, best_metric_BRATS, best_metric_epoch_BRATS
                        )
                    )
                    writer.add_scalar("val_mean_dice_BRATS", metric_BRATS, epoch_len * (epoch+1))
                    


                if "ATLAS" in dataset:

                    loader_index = data_loader_map["ATLAS"]
                    for val_data in val_loader_ATLAS:
                        # batch = val_data[loader_index]
                        if model_type == "UNET":
                            input_data = torch.from_numpy(np.zeros((1,len(total_modalities),val_data[0].shape[2],val_data[0].shape[3],val_data[0].shape[4]),dtype=np.float32))
                            input_data[:,ATLAS_channel_map,:,:,:] = val_data[0]
                        else:
                            input_data = val_data[0]
                        input_data = input_data.to(device)
                        label = val_data[1].to(device)
                        
                        roi_size = (cropped_input_size[0], cropped_input_size[1], cropped_input_size[2])
                        sw_batch_size = 1
                        val_outputs = sliding_window_inference(input_data, roi_size, sw_batch_size, model)
                        val_outputs = [post_trans(i) for i in decollate_batch(val_outputs)]
                        # compute metric for current iteration
                        dice_metric(y_pred=val_outputs, y=label)
                    metric_ATLAS = dice_metric.aggregate().item()
                    # reset the status for next validation round
                    dice_metric.reset()

                    if metric_ATLAS > best_metric_ATLAS:
                        best_metric_ATLAS = metric_ATLAS
                        best_metric_epoch_ATLAS = epoch + 1
                        if epoch>150:
                            model_save_name = model_save_path + save_name + "_BEST_ATLAS.pth"
                            torch.save(model.state_dict(), model_save_name)
                            print("saved new best metric model")
                    print(
                        "current epoch: {} current mean dice ATLAS: {:.4f} best mean dice ATLAS: {:.4f} at epoch {}".format(
                            epoch + 1, metric_ATLAS, best_metric_ATLAS, best_metric_epoch_ATLAS
                        )
                    )
                    writer.add_scalar("val_mean_dice_ATLAS", metric_ATLAS, epoch_len * (epoch+1))

                if "MSSEG" in dataset:

                    loader_index = data_loader_map["MSSEG"]
                    for val_data in val_loader_MSSEG:
                        # batch = val_data[loader_index]
                        if model_type == "UNET":
                            input_data = torch.from_numpy(np.zeros((1,len(total_modalities),val_data[0].shape[2],val_data[0].shape[3],val_data[0].shape[4]),dtype=np.float32))
                            input_data[:,MSSEG_channel_map,:,:,:] = val_data[0]
                        else:
                            input_data = val_data[0]
                        input_data = input_data.to(device)
                        label = val_data[1].to(device)
                        
                        roi_size = (cropped_input_size[0], cropped_input_size[1], cropped_input_size[2])
                        sw_batch_size = 1
                        val_outputs = sliding_window_inference(input_data, roi_size, sw_batch_size, model)
                        val_outputs = [post_trans(i) for i in decollate_batch(val_outputs)]
                        # compute metric for current iteration
                        dice_metric(y_pred=val_outputs, y=label)
                    metric_MSSEG = dice_metric.aggregate().item()
                    # reset the status for next validation round
                    dice_metric.reset()

                    if metric_MSSEG > best_metric_MSSEG:
                        best_metric_MSSEG = metric_MSSEG
                        best_metric_epoch_MSSEG = epoch + 1
                        if epoch>150:
                            model_save_name = model_save_path + save_name + "_BEST_MSSEG.pth"
                            torch.save(model.state_dict(), model_save_name)
                            print("saved new best metric model")
                    print(
                        "current epoch: {} current mean dice MSSEG: {:.4f} best mean dice MSSEG: {:.4f} at epoch {}".format(
                            epoch + 1, metric_MSSEG, best_metric_MSSEG, best_metric_epoch_MSSEG
                        )
                    )
                    writer.add_scalar("val_mean_dice_MSSEG", metric_MSSEG, epoch_len * (epoch+1))

                if "ISLES" in dataset:

                    loader_index = data_loader_map["ISLES"]
                    for val_data in val_loader_ISLES:
                        # batch = val_data[loader_index]
                        if model_type == "UNET":
                            input_data = torch.from_numpy(np.zeros((1,len(total_modalities),val_data[0].shape[2],val_data[0].shape[3],val_data[0].shape[4]),dtype=np.float32))
                            input_data[:,ISLES_channel_map,:,:,:] = val_data[0]
                        else:
                            input_data = val_data[0]
                        input_data = input_data.to(device)
                        label = val_data[1].to(device)
                        
                        roi_size = (cropped_input_size[0], cropped_input_size[1], cropped_input_size[2])
                        sw_batch_size = 1
                        val_outputs = sliding_window_inference(input_data, roi_size, sw_batch_size, model)
                        val_outputs = [post_trans(i) for i in decollate_batch(val_outputs)]
                        # compute metric for current iteration
                        dice_metric(y_pred=val_outputs, y=label)
                    metric_ISLES = dice_metric.aggregate().item()
                    # reset the status for next validation round
                    dice_metric.reset()

                    if metric_ISLES > best_metric_ISLES:
                        best_metric_ISLES = metric_ISLES
                        best_metric_epoch_ISLES = epoch + 1
                        if epoch>150:
                            model_save_name = model_save_path + save_name + "_BEST_ISLES.pth"
                            torch.save(model.state_dict(), model_save_name)
                            print("saved new best metric model")
                    print(
                        "current epoch: {} current mean dice ISLES: {:.4f} best mean dice ISLES: {:.4f} at epoch {}".format(
                            epoch + 1, metric_ISLES, best_metric_ISLES, best_metric_epoch_ISLES
                        )
                    )
                    writer.add_scalar("val_mean_dice_ISLES", metric_ISLES, epoch_len * (epoch+1))



                
                # scheduler.step(metric,epoch=epoch)
                writer.add_scalar("learning_rate", (optimizer.param_groups)[0]['lr'], epoch_len * (epoch+1))


    # print(f"train completed, best_metric: {best_metric:.4f} at epoch: {best_metric_epoch}")
    writer.close()

