import monai
from monai.networks.blocks import ResidualUnit
import torch
from UNetv2 import UNetv2

class Hemis_Net:

    def __init__(self, device):
        self.device = device
        # 16 outputs per UNet
        UNet_outs = 16
        conv1_in = 16
        conv1_out = 16
        conv2_in = 16
        conv2_out = 16
        conv3_in = 16

        print("CREATING HEMIS_NET just mean fusion")
        print("UNet_outs, conv1_in, conv1_out, conv2_in, conv2_out, conv3_in")
        print(UNet_outs)
        print(conv1_out)
        print(conv1_out)
        print(conv2_in)
        print(conv2_out)
        print(conv3_in)

        print("UNET layers:")
        print("Layers: 16,32,64,128,256")
        dropout = 0.2
        print("Dropout: ",dropout)
        print("res units: 2")
        # self.model_1 = monai.networks.nets.UNet(
        #     spatial_dims=3,
        #     in_channels=1,
        #     out_channels=UNet_outs,
        #     kernel_size = (3,3,3),
        #     channels=(16, 32, 64, 128, 256),
        #     strides=((2,2,2),(2,2,2),(2,2,2),(2,2,2)),
        #     num_res_units=2,
        #     dropout=dropout,
        # ).to(device)
        
        print("Using UNetv2")
        self.model_1 = UNetv2(
            spatial_dims=3,
            in_channels=1,
            out_channels=UNet_outs,
            kernel_size = (3,3,3),
            channels=(16, 32, 64, 128, 256),
            strides=((2,2,2),(2,2,2),(2,2,2),(2,2,2)),
            num_res_units=2,
            dropout=dropout,
        ).to(device)



        #  for residual units at post-fusion layer
        self.conv1 = ResidualUnit(
            spatial_dims=3,
            in_channels=conv1_in,
            out_channels=conv1_out,
        ).to(device)

        self.conv2 = ResidualUnit(
            spatial_dims=3,
            in_channels=conv2_in,
            out_channels=conv2_out,
        ).to(device)
        print("Segmenting Res Unit: in 16, out 1")
        self.conv3 = ResidualUnit(
            spatial_dims=3,
            in_channels=conv3_in,
            out_channels=1,
        ).to(device)
        

        self.parameters = list(self.model_1.parameters()) + list(self.conv1.parameters()) + list(self.conv2.parameters()) + list(self.conv3.parameters())

    def combined_model(self,batch_data):
        outs = []
        number_of_modalities = len(batch_data[0])

        for modality_index in range(number_of_modalities):
            modality_input = (batch_data[:,[modality_index],:,:,:]).to(self.device)
            modality_out = self.model_1(modality_input)
            outs.append(modality_out)


        if number_of_modalities != 1:
            outputs_tensor = torch.stack(outs)
            # print(outputs_tensor.shape)
            means = torch.mean(outputs_tensor,dim=0)
            # print(means.shape)
        else:
            means = outs[0]


        final_input = means.to(self.device)
        # del means
        # del vars

        # out_conv_1 = self.conv1(final_input)
        # out_conv_2 = self.conv2(out_conv_1)
        final_outputs = self.conv3(self.conv2(self.conv1(final_input)))

        return final_outputs

    def eval(self):
        self.model_1.eval()
        self.conv1.eval()
        self.conv2.eval()
        self.conv3.eval()

    def train(self):
        self.model_1.train()
        self.conv1.train()
        self.conv2.train()
        self.conv3.train()

    def load(self,load_path):
        initial_model_path = load_path + "_initial.pth"
        conv1_path = load_path + "_conv1.pth"
        conv2_path = load_path + "_conv2.pth"
        conv3_path = load_path + "_conv3.pth"

        # print(self.device)
        
        # print(self.device.index)
        
        # cuda_id = "cuda:" + str(self.device.index)
        # print(cuda_id)
        cuda_id = str(self.device)

        self.model_1.load_state_dict(torch.load(initial_model_path,map_location={"cuda:0":cuda_id,"cuda:1":cuda_id}))
        self.conv1.load_state_dict(torch.load(conv1_path,map_location={"cuda:0":cuda_id,"cuda:1":cuda_id}))
        self.conv2.load_state_dict(torch.load(conv2_path,map_location={"cuda:0":cuda_id,"cuda:1":cuda_id}))
        self.conv3.load_state_dict(torch.load(conv3_path,map_location={"cuda:0":cuda_id,"cuda:1":cuda_id}))
