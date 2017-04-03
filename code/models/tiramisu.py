from __future__ import print_function

import numpy as np

from keras import backend as K
from keras.layers import Input, merge
from keras.layers.convolutional import Convolution2D, MaxPooling2D

from keras.layers.core import Dropout
from keras.models import Model
from keras.regularizers import l2
from keras.layers import Activation, BatchNormalization, MaxPooling2D

from code.layers.ourlayers import NdSoftmax
from code.layers.deconv import Deconvolution2D

# Batch normalization dimensions
dim_ordering = K.image_dim_ordering()
if dim_ordering == "th":
    bn_axis = 1
else:
    bn_axis = -1

# Concatenation axis
if K.image_dim_ordering() == "th":
    concat_axis = 1
else:
    concat_axis = -1

""" MODEL BUILDERS """
"""
Paper: https://arxiv.org/abs/1611.09326
Implementation based on the following Theano / Lasagne code: https://github.com/SimJeg/FC-DenseNet
"""


def build_tiramisu_fc67(img_shape=(None, None, 3), nclasses=8, weight_decay=1e-4, compression=0, dropout=0.2,
                        freeze_layers_from=None, nb_filter=48):
    # Parameters of the network
    n_layers_block = [5] * 11  # Dense layers per dense block
    growth_rate = 16  # Growth rate of dense blocks, k in DenseNet paper
    compression = 1 - compression  # Compression factor applied in Transition Down (only in case of OOM problems)

    tiramisu_model, network = tiramisu_network(
        img_shape, n_layers_block, growth_rate, nclasses, weight_decay,
        compression, dropout, 'Tiramisu_FC67', freeze_layers_from, nb_filter
    )

    return tiramisu_model


def build_tiramisu_fc103(img_shape=(None, None, 3), nclasses=8, weight_decay=1e-4, compression=0, dropout=0.2,
                         freeze_layers_from=None,
                         nb_filter=48):
    # Parameters of the network
    n_layers_block = [4, 5, 7, 10, 12, 15, 12, 10, 7, 5, 4]  # Dense layers per dense block
    growth_rate = 16  # Growth rate of dense blocks, k in DenseNet paper
    compression = 1 - compression  # Compression factor applied in Transition Down (only in case of OOM problems)

    tiramisu_model, network = tiramisu_network(
        img_shape, n_layers_block, growth_rate, nclasses, weight_decay,
        compression, dropout, 'Tiramisu_FC103', freeze_layers_from, nb_filter
    )

    return tiramisu_model


""" BUILDING BLOCKS """


def tiramisu_network(img_shape, n_layers_block, growth_rate,
                     nclasses, weight_decay, compression, dropout, network_name,
                     freeze_layers_from=None, nb_filter=48):
    """
    Creates a Keras model that represents the Tiramisu network specefied according to the number of layers per dense 
    block, the index where the transition from downsampling to upsampling occurs, the growth rate, and other parameters
    related to DenseNet and DNN models in general.
    
    :param img_shape: shape of the input image (e.g. (3, 300, 300) for th backend, (300, 300, 3) for tf)
    :param n_layers_block: list that specifies the number of layers per each dense block, including the dense blocks
    from the downsampling path, the upsampling path and the transition dense block. The length of this list must be
    an even number.
    :param growth_rate: Growth rate (for more details see DenseNet).
    :param nclasses: Number of classes of the segmentation, including background
    :param weight_decay: Amount of L2 norm penalization applied to the weights
    :param compression: Compression factor for DenseNet
    :param dropout: Dropout rate for conv layers.
    :param network_name: Name of the network
    :param freeze_layers_from: The first layers that won't be updated 
    :param nb_filter: Number of kernels in the first convolution
    :return: Keras model and network dictionary with all the feature maps
    :rtype: tuple
    """
    # Placeholder for the feature maps in each dense block, transition down or transition up
    net = dict()

    # Placeholder for skip connections
    skip_connection = list()

    # Number of layers per block must be odd
    assert (len(n_layers_block) - 1) % 2 == 0

    # Transition index
    transition_index = int(np.floor(len(n_layers_block) / 2))
    print('Total number of blocks: {}   Transition block index: {}'.format(len(n_layers_block), transition_index))

    # Layers per block for the 3 main structures: downsampling path, transition and upsampling path
    down_layers_block = n_layers_block[:transition_index]
    transition_layers_block = n_layers_block[transition_index]
    up_layers_block = n_layers_block[transition_index + 1:]

    assert len(down_layers_block) == len(up_layers_block)

    # Initial convolution
    net['input'] = Input(shape=img_shape)
    x = Convolution2D(nb_filter, 3, 3,
                      init="he_uniform",
                      border_mode="same",
                      name="initial_conv2D",
                      W_regularizer=l2(weight_decay))(net['input'])
    net['init_conv'] = x

    # Dense blocks + Transition down in the downsampling path
    for block_idx, n_layers_block in enumerate(down_layers_block):
        # Dense block
        x, nb_filter = denseblock(x, nb_layers=n_layers_block,
                                  nb_filter=nb_filter, growth_rate=growth_rate,
                                  dropout_rate=dropout,
                                  weight_decay=weight_decay,
                                  stack_input=True)
        feature_name = 'db_{}'.format(block_idx)
        net[feature_name] = x
        skip_connection.append(x)

        # Compression
        nb_filter = int(compression * nb_filter)

        # Transition Down
        x = transition_down(x, nb_filter, dropout_rate=dropout, weight_decay=weight_decay)
        feature_name = 'td_{}'.format(block_idx)
        net[feature_name] = x

    # Reverse skip connection list
    skip_connection = skip_connection[::-1]

    # The last denseblock does not have a transition down and does not stack the input
    x, nb_filter = denseblock(x, nb_layers=transition_layers_block,
                              nb_filter=nb_filter, growth_rate=growth_rate,
                              dropout_rate=dropout,
                              weight_decay=weight_decay,
                              stack_input=False)
    feature_name = 'db_{}'.format(transition_index)
    net[feature_name] = x

    # Upsampling path
    x_up = x
    for block_idx, n_layers_block in enumerate(up_layers_block):
        # Number of filters to be keeped
        keep_filters = growth_rate * n_layers_block

        # Skip connection related to this block
        skip = skip_connection[block_idx]
        x_up = transition_up(x, skip, keep_filters, weight_decay=weight_decay)
        feature_name = 'tu_{}'.format(block_idx)
        net[feature_name] = x_up

        # Dense block
        x, _ = denseblock(x_up, n_layers_block,
                          nb_filter=0, growth_rate=growth_rate,
                          dropout_rate=dropout,
                          weight_decay=weight_decay,
                          stack_input=False)
        feature_name = 'db_up_{}'.format(block_idx)
        net[feature_name] = x

    # Softmax
    net['output_features'] = merge([x_up, skip_connection], mode='concat', concat_axis=concat_axis)
    net['pixel_class'] = Convolution2D(nclasses, 1, 1,
                                       init='he_uniform',
                                       border_mode='same',
                                       W_regularizer=l2(weight_decay),
                                       b_regularizer=l2(weight_decay))(net['output_features'])
    net['softmax'] = NdSoftmax()(net['softmax'])

    # Model
    tiramisu_model = Model(input=[net['input']], output=[net['softmax']], name=network_name)

    # Freeze some layers
    if freeze_layers_from is not None:
        if freeze_layers_from == 'base_model':
            raise ValueError('Freezing the base_model is not supported for network {}'.format(network_name))
        else:
            for i, layer in enumerate(model.layers):
                print(i, layer.name)
            print('   Freezing from layer 0 to ' + str(freeze_layers_from))
            for layer in model.layers[:freeze_layers_from]:
                layer.trainable = False
            for layer in model.layers[freeze_layers_from:]:
                layer.trainable = True

    return tiramisu_model, net


def conv_factory(x, nb_filter, dropout_rate=None, weight_decay=1E-4):
    """Apply BatchNorm, Relu 3x3Conv2D, optional dropout
    :param x: Input keras network
    :param nb_filter: int -- number of filters
    :param dropout_rate: int -- dropout rate
    :param weight_decay: int -- weight decay factor
    :returns: keras network with b_norm, relu and convolution2d added
    :rtype: keras network
    """
    x = BatchNormalization(mode=0,
                           axis=bn_axis,
                           gamma_regularizer=l2(weight_decay),
                           beta_regularizer=l2(weight_decay))(x)
    x = Activation('relu')(x)
    x = Convolution2D(nb_filter, 3, 3,
                      init="he_uniform",
                      border_mode="same",
                      W_regularizer=l2(weight_decay),
                      b_regularizer=l2(weight_decay))(x)
    if dropout_rate:
        x = Dropout(dropout_rate)(x)

    return x


def denseblock(x, nb_layers, nb_filter, growth_rate,
               dropout_rate=None, weight_decay=1e-4, stack_input=True):
    """Build a denseblock where the output of each
       conv_factory is fed to subsequent ones
    :param x: keras model
    :param nb_layers: int -- the number of layers of conv_
                      factory to append to the model.
    :param nb_filter: int -- number of filters
    :param growth_rate: int -- growth rate
    :param dropout_rate: int -- dropout rate
    :param weight_decay: int -- weight decay factor
    :param stack_input: bool -- include the input with the stacked feature maps 
    :returns: keras model with nb_layers of conv_factory appended
    :rtype: keras model
    """

    # List of concatenated features. The input is optional.
    list_feat = []
    if stack_input:
        list_feat = [x]

    for i in range(nb_layers):
        x = conv_factory(x, nb_filter=growth_rate, dropout_rate=dropout_rate, weight_decay=weight_decay)
        list_feat.append(x)
        if len(list_feat) > 1:
            x = merge(list_feat, mode='concat', concat_axis=concat_axis)
        nb_filter += growth_rate

    return x, nb_filter


def transition_down(x, nb_filter, dropout_rate=None, weight_decay=1E-4):
    """Apply BatchNorm, Relu 1x1Conv2D, optional dropout and Maxpooling2D
    :param x: keras model
    :param nb_filter: int -- number of filters
    :param dropout_rate: int -- dropout rate
    :param weight_decay: int -- weight decay factor
    :returns: model
    :rtype: keras model, after applying batch_norm, relu-conv, dropout, maxpool
    """
    x = BatchNormalization(mode=0,
                           axis=bn_axis,
                           gamma_regularizer=l2(weight_decay),
                           beta_regularizer=l2(weight_decay))(x)
    x = Activation('relu')(x)
    x = Convolution2D(nb_filter, 1, 1,
                      init="he_uniform",
                      border_mode="same",
                      W_regularizer=l2(weight_decay),
                      b_regularizer=l2(weight_decay))(x)
    if dropout_rate:
        x = Dropout(dropout_rate)(x)
    x = MaxPooling2D((2, 2), strides=(2, 2))(x)

    return x


def transition_up(x, skip_connection, keep_filters, weight_decay=1E-4):
    """Apply BatchNorm, Relu 1x1Conv2D, optional dropout and Maxpooling2D
    :param x: keras model
    :param skip_connection: skip connection feature map from downsampling path
    :param keep_filters: number of filters to be convolved with
    :param weight_decay: int -- weight decay factor
    :returns: model
    :rtype: keras model, after applying batch_norm, relu-conv, dropout, maxpool
    """
    # Transposed convolution
    deconv = Deconvolution2D(keep_filters, 3, 3,
                             input_shape=x._keras_shape,
                             init='he_uniform',
                             subsample=(2, 2),
                             W_regularizer=l2(weight_decay),
                             b_regularizer=l2(weight_decay))(x)

    return merge([deconv, skip_connection], mode='concat', concat_axis=concat_axis)


if __name__ == '__main__':
    input_shape = (320, 320, 3)
    print(' > Building Tiramisu FC67')
    model = build_tiramisu_fc67(input_shape, nclasses=11, weight_decay=1e-4, dropout=0.2)
    print(' > Compiling Tiramisu FC67')
    model.compile(loss="categorical_crossentropy", optimizer="rmsprop")
    model.summary()

    print(' > Building Tiramisu FC103')
    model = build_tiramisu_fc103(input_shape, nclasses=11, weight_decay=1e-4, dropout=0.2)
    print(' > Compiling Tiramisu FC103')
    model.compile(loss="categorical_crossentropy", optimizer="rmsprop")
    model.summary()
