# Copyright 2020 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Tests for automatic outside compilation for TF 2.0/Keras."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf

import os

from absl import flags
import numpy as np

from tensorboard.plugins.histogram import summary_v2 as histogram_summary_v2
from tensorboard.plugins.image import summary_v2 as image_summary_v2
from tensorboard.plugins.scalar import summary_v2 as scalar_summary_v2
from tensorflow.python.eager.context import set_soft_device_placement
from keras import callbacks
from keras import initializers
from keras.distribute import distribute_strategy_test
from keras.engine import base_layer
from keras.engine import sequential as sequential_model_lib
from keras.engine import training
from keras.layers import convolutional as conv_layer_lib
from keras.layers import core as layer_lib
from keras.layers import pooling as pool_layer_lib

NUM_CLASSES = 4

FLAGS = flags.FLAGS
flags.DEFINE_string('tpu', '', 'Name of TPU to connect to.')
flags.DEFINE_string('project', None, 'Name of GCP project with TPU.')
flags.DEFINE_string('zone', None, 'Name of GCP zone with TPU.')


def get_tpu_cluster_resolver():
  resolver = tf.distribute.cluster_resolver.TPUClusterResolver(
      tpu=FLAGS.tpu,
      zone=FLAGS.zone,
      project=FLAGS.project,
  )
  return resolver


def get_tpu_strategy():
  resolver = get_tpu_cluster_resolver()
  tf.config.experimental_connect_to_cluster(resolver)
  tf.tpu.experimental.initialize_tpu_system(resolver)
  return tf.distribute.experimental.TPUStrategy(resolver)


class LayerForScalarSummary(base_layer.Layer):
  """A pass-through layer that only records scalar values to summary."""

  def call(self, x):
    # Add summary scalar using compat v2 implementation.
    scalar_summary_v2.scalar('custom_scalar_summary_v2', tf.reduce_sum(x))
    return x


class LayerForImageSummary(base_layer.Layer):
  """A pass-through layer that only records image values to summary."""

  def call(self, x):
    # Add summary image using compat v2 implementation.
    image_summary_v2.image('custom_image_summary_v2', x)

    return x


class LayerForHistogramSummary(base_layer.Layer):
  """A pass-through layer that records histogram values to summary."""

  def call(self, x):
    # Add summary histogram using compat v2 implementation.
    histogram_summary_v2.histogram('custom_histogram_summary_v2', x)

    return x


class CustomModel(training.Model):
  """Custom model with summary ops in model call definition."""

  def __init__(self, name=None):
    super(CustomModel, self).__init__()
    self._my_layers = [
        layer_lib.Dense(
            4096,
            name='dense1',
            kernel_initializer=tf.compat.v1.glorot_normal_initializer(seed=0),
            use_bias=False),
        layer_lib.Dense(
            4,
            name='dense2',
            kernel_initializer=tf.compat.v1.glorot_normal_initializer(seed=0),
            use_bias=False),
    ]
    self.histogram_summary_layer = LayerForHistogramSummary()
    self.scalar_summary_layer = LayerForScalarSummary()

  def call(self, x):
    for layer in self._my_layers:
      x = layer(x)
    x = self.scalar_summary_layer(x)
    return self.histogram_summary_layer(x)


def get_image_dataset():
  inputs = np.zeros((10, 28, 28, 3), dtype=np.float32)
  targets = np.zeros((10, NUM_CLASSES), dtype=np.float32)
  dataset = tf.data.Dataset.from_tensor_slices((inputs, targets))
  dataset = dataset.repeat(100)
  dataset = dataset.batch(10, drop_remainder=True)
  return dataset


def mnist_model(input_shape):
  """Creates a MNIST model."""
  model = sequential_model_lib.Sequential()

  # Adding custom pass-through layer to visualize input images.
  model.add(LayerForImageSummary())

  model.add(
      conv_layer_lib.Conv2D(
          32, kernel_size=(3, 3), activation='relu', input_shape=input_shape))
  model.add(conv_layer_lib.Conv2D(64, (3, 3), activation='relu'))
  model.add(pool_layer_lib.MaxPooling2D(pool_size=(2, 2)))
  model.add(layer_lib.Dropout(0.25))
  model.add(layer_lib.Flatten())
  model.add(layer_lib.Dense(128, activation='relu'))
  model.add(layer_lib.Dropout(0.5))
  model.add(layer_lib.Dense(NUM_CLASSES, activation='softmax'))

  # Adding custom pass-through layer for summary recording.
  model.add(LayerForHistogramSummary())
  return model


class AutoOutsideCompilationWithKerasTest(tf.test.TestCase):

  def setUp(self):
    super(AutoOutsideCompilationWithKerasTest, self).setUp()
    tf.compat.v1.enable_v2_behavior()
    set_soft_device_placement(True)
    self.summary_dir = self.get_temp_dir()

  def validate_recorded_sumary_file(self, event_files, summary_dict,
                                    expected_count):
    for event_file in event_files:
      for e in tf.compat.v1.train.summary_iterator(event_file):
        for v in e.summary.value:
          if v.tag in summary_dict:
            summary_dict[v.tag] += 1

    for key in summary_dict:
      self.assertEqual(summary_dict[key], expected_count)

  def testV2SummaryWithKerasSequentialModel(self):
    strategy = get_tpu_strategy()

    with strategy.scope():
      model = mnist_model((28, 28, 3))
      model.compile('sgd', 'mse')

      dataset = get_image_dataset()
      tensorboard_callback = callbacks.TensorBoard(
          self.summary_dir, update_freq=2)
      model.fit(
          dataset,
          steps_per_epoch=10,
          epochs=1,
          callbacks=[tensorboard_callback])

      events_count_dictionary = {
          'sequential/layer_for_histogram_summary/custom_histogram_summary_v2':
              0,
          'sequential/layer_for_image_summary/custom_image_summary_v2':
              0,
      }

      event_files = tf.io.gfile.glob(
          os.path.join(self.summary_dir, 'train', 'event*'))
      # Since total of 10 steps are ran and summary ops should be invoked
      # every 2 batches, we should see total of 5 event logs.
      self.validate_recorded_sumary_file(event_files, events_count_dictionary,
                                         5)

  def testV2SummaryWithKerasSubclassedModel(self):
    strategy = get_tpu_strategy()

    with strategy.scope():
      model = CustomModel()
      model.compile('sgd', 'mse')

      dataset = distribute_strategy_test.get_dataset(strategy)
      tensorboard_callback = callbacks.TensorBoard(
          self.summary_dir, update_freq=2)
      model.fit(
          dataset,
          steps_per_epoch=10,
          epochs=1,
          callbacks=[tensorboard_callback])

      event_files = tf.io.gfile.glob(
          os.path.join(self.summary_dir, 'train', 'event*'))
      events_count_dictionary = {
          ('custom_model/layer_for_scalar_summary/'
           'custom_scalar_summary_v2'):
              0,
          ('custom_model/layer_for_histogram_summary/'
           'custom_histogram_summary_v2'):
              0
      }

      # Since total of 10 steps are ran and summary ops should be invoked
      # every 2 batches, we should see total of 5 event logs.
      self.validate_recorded_sumary_file(event_files, events_count_dictionary,
                                         5)

  def testSummaryWithCustomTrainingLoop(self):
    strategy = get_tpu_strategy()

    writer = tf.summary.create_file_writer(self.summary_dir)
    with strategy.scope():
      model = distribute_strategy_test.get_model()
      model.compile('sgd', 'mse')

    @tf.function
    def custom_function(dataset):

      def _custom_step(features, labels):
        del labels
        logits = model(features)
        with tf.summary.record_if(True), writer.as_default():
          scalar_summary_v2.scalar(
              'logits',
              tf.reduce_sum(logits),
              step=model.optimizer.iterations)
        return logits

      iterator = iter(dataset)
      output = strategy.unwrap(
          strategy.run(_custom_step, args=(next(iterator))))
      return output

    dataset = strategy.experimental_distribute_dataset(
        distribute_strategy_test.get_dataset(strategy))

    custom_function(dataset)
    writer.close()

    event_files = tf.io.gfile.glob(
        os.path.join(self.summary_dir, 'event*'))
    events_count_dictionary = {
        ('logits'): 0,
    }
    self.validate_recorded_sumary_file(event_files, events_count_dictionary,
                                       1)


if __name__ == '__main__':
  tf.compat.v1.enable_eager_execution()
  tf.test.main()
