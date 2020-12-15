# Copyright 2019 The TensorFlow Authors. All Rights Reserved.
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
"""Tests for keras.layers.preprocessing.normalization."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf

import numpy as np

import keras
from keras import keras_parameterized
from keras.distribute import strategy_combinations
from keras.layers.preprocessing import category_encoding
from keras.layers.preprocessing import preprocessing_test_utils


def batch_wrapper(dataset, batch_size, distribution, repeat=None):
  if repeat:
    dataset = dataset.repeat(repeat)
  # TPUs currently require fully defined input shapes, drop_remainder ensures
  # the input will have fully defined shapes.
  if isinstance(distribution,
                (tf.distribute.experimental.TPUStrategy, tf.compat.v1.distribute.experimental.TPUStrategy)):
    return dataset.batch(batch_size, drop_remainder=True)
  else:
    return dataset.batch(batch_size)


@tf.__internal__.distribute.combinations.generate(
    tf.__internal__.test.combinations.combine(
        # (b/156783625): Outside compilation failed for eager mode only.
        distribution=strategy_combinations.strategies_minus_tpu,
        mode=["eager", "graph"]))
class CategoryEncodingDistributionTest(
    keras_parameterized.TestCase,
    preprocessing_test_utils.PreprocessingLayerTest):

  def test_distribution(self, distribution):
    input_array = np.array([[1, 2, 3, 1], [0, 3, 1, 0]])
    inp_dataset = tf.data.Dataset.from_tensor_slices(input_array)
    inp_dataset = batch_wrapper(inp_dataset, 2, distribution)

    # pyformat: disable
    expected_output = [[0, 1, 1, 1, 0, 0],
                       [1, 1, 0, 1, 0, 0]]
    # pyformat: enable
    max_tokens = 6
    tf.config.set_soft_device_placement(True)

    with distribution.scope():
      input_data = keras.Input(shape=(4,), dtype=tf.int32)
      layer = category_encoding.CategoryEncoding(
          max_tokens=max_tokens, output_mode=category_encoding.BINARY)
      int_data = layer(input_data)
      model = keras.Model(inputs=input_data, outputs=int_data)
    output_dataset = model.predict(inp_dataset)
    self.assertAllEqual(expected_output, output_dataset)


if __name__ == "__main__":
  tf.test.main()
