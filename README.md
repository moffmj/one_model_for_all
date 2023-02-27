Creating a CNN model architecture that can handle any input modalities for biomedical MRI segmentation. Currently networks are trained on datasets individually - e.g. on Brain Tumour separately to stroke. This ignores the possiblility that there may be potential to learn inherent features of "healthy" and "unhealthy" matter. The main problem with this is that MRIs come in multiple different modalities. Most network architectures require this channel dimensionality to be predefined at training. We would like to make use of modalities never seen at training and be able to take in an undefined number of modalities.