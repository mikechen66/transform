# coding=utf-8
#
# Copyright 2017 Google Inc. All Rights Reserved.
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
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import contextlib
import itertools
import math
import os
import random

# GOOGLE-INITIALIZATION

import apache_beam as beam
from apache_beam.testing import util as beam_test_util
import numpy as np
import six
from six.moves import range

import tensorflow as tf
import tensorflow_transform as tft
from tensorflow_transform import analyzers
from tensorflow_transform import schema_inference
from tensorflow_transform.beam import impl as beam_impl
from tensorflow_transform.beam import tft_unit
from tensorflow_transform.beam.tft_beam_io import transform_fn_io
from google.protobuf import text_format
from tensorflow.contrib.proto.python.ops import encode_proto_op
from tensorflow.core.example import example_pb2
from tensorflow.python.ops import lookup_ops
from tensorflow_metadata.proto.v0 import schema_pb2


def _construct_test_bucketization_parameters():
  args_without_dtype = (
      (range(1, 10), [4, 7], False, None, False, False),
      (range(1, 100), [26, 51, 76], False, None, False, False),

      # The following is similar to range(1, 100) test above, except that
      # only odd numbers are in the input; so boundaries differ (26 -> 27 and
      # 76 -> 77).
      (range(1, 100, 2), [27, 51, 77], False, None, False, False),

      # Test some inversely sorted inputs, and with different strides, and
      # boundaries/buckets.
      (range(9, 0, -1), [4, 7], False, None, False, False),
      (range(19, 0, -1), [11], False, None, False, False),
      (range(99, 0, -1), [51], False, None, False, False),
      (range(99, 0, -1), [34, 67], False, None, False, False),
      (range(99, 0, -2), [34, 68], False, None, False, False),
      (range(99, 0, -1), range(11, 100, 10), False, None, False, False),

      # These tests do a random shuffle of the inputs, which must not affect the
      # boundaries (or the computed buckets).
      (range(99, 0, -1), range(11, 100, 10), True, None, False, False),
      (range(1, 100), range(11, 100, 10), True, None, False, False),

      # The following test is with multiple batches (3 batches with default
      # batch of 1000).
      (range(1, 3000), [1503], False, None, False, False),
      (range(1, 3000), [1001, 2001], False, None, False, False),

      # Test with specific error for bucket boundaries. This is same as the test
      # above with 3 batches and a single boundary, but with a stricter error
      # tolerance (0.001) than the default error (0.01). The result is that the
      # computed boundary in the test below is closer to the middle (1501) than
      # that computed by the boundary of 1503 above.
      (range(1, 3000), [1501], False, 0.001, False, False),

      # Test with specific error for bucket boundaries, with more relaxed error
      # tolerance (0.1) than the default (0.01). Now the boundary diverges
      # further to 1519 (compared to boundary of 1501 with error 0.001, and
      # boundary of 1503 with error 0.01).
      (range(1, 3000), [1519], False, 0.1, False, False),

      # Tests for tft.apply_buckets.
      (range(1, 100), [26, 51, 76], False, 0.00001, True, False),
      # TODO(b/78569039): Enable this test.
      # (range(1, 100), [26, 51, 76], False, 0.00001, True, True),
  )
  dtypes = (tf.int32, tf.int64, tf.float16, tf.float32, tf.float64, tf.double)
  return (x + (dtype,) for x in args_without_dtype for dtype in dtypes)


def _canonical_dtype(dtype):
  """Returns int64 for int dtypes and float32 for float dtypes."""
  if dtype.is_floating:
    return tf.float32
  elif dtype.is_integer:
    return tf.int64
  else:
    raise ValueError('Bad dtype {}'.format(dtype))


def sum_output_dtype(input_dtype):
  """Returns the output dtype for tft.sum."""
  return input_dtype if input_dtype.is_floating else tf.int64


def _mean_output_dtype(input_dtype):
  """Returns the output dtype for tft.mean (and similar functions)."""
  return tf.float64 if input_dtype == tf.float64 else tf.float32


class BeamImplTest(tft_unit.TransformTestCase):

  def setUp(self):
    tf.compat.v1.logging.info('Starting test case: %s', self._testMethodName)

    self._context = beam_impl.Context(use_deep_copy_optimization=True)
    self._context.__enter__()

  def tearDown(self):
    self._context.__exit__()

  def testApplySavedModelSingleInput(self):
    def save_model_with_single_input(instance, export_dir):
      builder = tf.compat.v1.saved_model.builder.SavedModelBuilder(export_dir)
      with instance.test_session(graph=tf.Graph()) as sess:
        input1 = tf.compat.v1.placeholder(
            dtype=tf.int64, shape=[3], name='myinput1')
        initializer = tf.compat.v1.constant_initializer([1, 2, 3])
        with tf.compat.v1.variable_scope(
            'Model', reuse=None, initializer=initializer):
          v1 = tf.compat.v1.get_variable('v1', [3], dtype=tf.int64)
        output1 = tf.add(v1, input1, name='myadd1')
        inputs = {'single_input': input1}
        outputs = {'single_output': output1}
        signature_def_map = {
            'serving_default':
                tf.compat.v1.saved_model.signature_def_utils
                .predict_signature_def(inputs, outputs)
        }
        sess.run(tf.compat.v1.global_variables_initializer())
        builder.add_meta_graph_and_variables(
            sess, [tf.saved_model.SERVING], signature_def_map=signature_def_map)
        builder.save(False)

    export_dir = os.path.join(self.get_temp_dir(), 'saved_model_single')

    def preprocessing_fn(inputs):
      x = inputs['x']
      output_col = tft.apply_saved_model(
          export_dir, x, tags=[tf.saved_model.SERVING])
      return {'out': output_col}

    save_model_with_single_input(self, export_dir)
    input_data = [
        {'x': [1, 2, 3]},
    ]
    input_metadata = tft_unit.metadata_from_feature_spec({
        'x': tf.io.FixedLenFeature([3], tf.int64),
    })
    # [1, 2, 3] + [1, 2, 3] = [2, 4, 6]
    expected_data = [
        {'out': [2, 4, 6]}
    ]
    expected_metadata = tft_unit.metadata_from_feature_spec(
        {'out': tf.io.FixedLenFeature([3], tf.int64)})
    self.assertAnalyzeAndTransformResults(
        input_data, input_metadata, preprocessing_fn, expected_data,
        expected_metadata)

  def testApplySavedModelWithHashTable(self):
    def save_model_with_hash_table(instance, export_dir):
      builder = tf.compat.v1.saved_model.builder.SavedModelBuilder(export_dir)
      with instance.test_session(graph=tf.Graph()) as sess:
        key = tf.constant('test_key', shape=[1])
        value = tf.constant('test_value', shape=[1])
        table = lookup_ops.HashTable(
            lookup_ops.KeyValueTensorInitializer(key, value), '__MISSING__')

        input1 = tf.compat.v1.placeholder(
            dtype=tf.string, shape=[1], name='myinput')
        output1 = tf.reshape(table.lookup(input1), shape=[1])
        inputs = {'input': input1}
        outputs = {'output': output1}

        signature_def_map = {
            'serving_default':
                tf.compat.v1.saved_model.signature_def_utils
                .predict_signature_def(inputs, outputs)
        }

        sess.run(table.init)
        builder.add_meta_graph_and_variables(
            sess, [tf.saved_model.SERVING], signature_def_map=signature_def_map)
        builder.save(False)

    export_dir = os.path.join(self.get_temp_dir(), 'saved_model_hash_table')

    def preprocessing_fn(inputs):
      x = inputs['x']
      output_col = tft.apply_saved_model(
          export_dir, x, tags=[tf.saved_model.SERVING])
      return {'out': output_col}

    save_model_with_hash_table(self, export_dir)
    input_data = [
        {'x': ['test_key']}
    ]
    input_metadata = tft_unit.metadata_from_feature_spec({
        'x': tf.io.FixedLenFeature([1], tf.string),
    })
    expected_data = [
        {'out': b'test_value'}
    ]
    expected_metadata = tft_unit.metadata_from_feature_spec(
        {'out': tf.io.FixedLenFeature([], tf.string)})
    self.assertAnalyzeAndTransformResults(
        input_data, input_metadata, preprocessing_fn, expected_data,
        expected_metadata)

  def testApplySavedModelMultiInputs(self):

    def save_model_with_multi_inputs(instance, export_dir):
      builder = tf.compat.v1.saved_model.builder.SavedModelBuilder(export_dir)
      with instance.test_session(graph=tf.Graph()) as sess:
        input1 = tf.compat.v1.placeholder(
            dtype=tf.int64, shape=[3], name='myinput1')
        input2 = tf.compat.v1.placeholder(
            dtype=tf.int64, shape=[3], name='myinput2')
        input3 = tf.compat.v1.placeholder(
            dtype=tf.int64, shape=[3], name='myinput3')
        initializer = tf.compat.v1.constant_initializer([1, 2, 3])
        with tf.compat.v1.variable_scope(
            'Model', reuse=None, initializer=initializer):
          v1 = tf.compat.v1.get_variable('v1', [3], dtype=tf.int64)
        o1 = tf.add(v1, input1, name='myadd1')
        o2 = tf.subtract(o1, input2, name='mysubtract1')
        output1 = tf.add(o2, input3, name='myadd2')
        inputs = {'name1': input1, 'name2': input2,
                  'name3': input3}
        outputs = {'single_output': output1}
        signature_def_map = {
            'serving_default':
                tf.compat.v1.saved_model.signature_def_utils
                .predict_signature_def(inputs, outputs)
        }
        sess.run(tf.compat.v1.global_variables_initializer())
        builder.add_meta_graph_and_variables(
            sess, [tf.saved_model.SERVING], signature_def_map=signature_def_map)
        builder.save(False)

    export_dir = os.path.join(self.get_temp_dir(), 'saved_model_multi')

    def preprocessing_fn(inputs):
      x = inputs['x']
      y = inputs['y']
      z = inputs['z']
      sum_column = tft.apply_saved_model(
          export_dir, {
              'name1': x,
              'name3': z,
              'name2': y
          },
          tags=[tf.saved_model.SERVING])
      return {'sum': sum_column}

    save_model_with_multi_inputs(self, export_dir)
    input_data = [
        {'x': [1, 2, 3], 'y': [2, 3, 4], 'z': [1, 1, 1]},
    ]
    input_metadata = tft_unit.metadata_from_feature_spec({
        'x': tf.io.FixedLenFeature([3], tf.int64),
        'y': tf.io.FixedLenFeature([3], tf.int64),
        'z': tf.io.FixedLenFeature([3], tf.int64),
    })
    # [1, 2, 3] + [1, 2, 3] - [2, 3, 4] + [1, 1, 1] = [1, 2, 3]
    expected_data = [
        {'sum': [1, 2, 3]}
    ]
    expected_metadata = tft_unit.metadata_from_feature_spec(
        {'sum': tf.io.FixedLenFeature([3], tf.int64)})
    self.assertAnalyzeAndTransformResults(
        input_data, input_metadata, preprocessing_fn, expected_data,
        expected_metadata)

  def testApplyFunctionWithCheckpoint(self):

    def tensor_fn(input1, input2):
      initializer = tf.compat.v1.constant_initializer([1, 2, 3])
      with tf.compat.v1.variable_scope(
          'Model', reuse=None, initializer=initializer):
        v1 = tf.compat.v1.get_variable('v1', [3], dtype=tf.int64)
        v2 = tf.compat.v1.get_variable('v2', [3], dtype=tf.int64)
        o1 = tf.add(v1, v2, name='add1')
        o2 = tf.subtract(o1, input1, name='sub1')
        o3 = tf.subtract(o2, input2, name='sub2')
        return o3

    def save_checkpoint(instance, checkpoint_path):
      with instance.test_session(graph=tf.Graph()) as sess:
        input1 = tf.compat.v1.placeholder(
            dtype=tf.int64, shape=[3], name='myinput1')
        input2 = tf.compat.v1.placeholder(
            dtype=tf.int64, shape=[3], name='myinput2')
        tensor_fn(input1, input2)
        saver = tf.compat.v1.train.Saver()
        sess.run(tf.compat.v1.global_variables_initializer())
        saver.save(sess, checkpoint_path)

    checkpoint_path = os.path.join(self.get_temp_dir(), 'chk')

    def preprocessing_fn(inputs):
      x = inputs['x']
      y = inputs['y']
      out_value = tft.apply_function_with_checkpoint(
          tensor_fn, [x, y], checkpoint_path)
      return {'out': out_value}

    save_checkpoint(self, checkpoint_path)
    input_data = [
        {'x': [2, 2, 2], 'y': [-1, -3, 1]},
    ]
    input_metadata = tft_unit.metadata_from_feature_spec({
        'x': tf.io.FixedLenFeature([3], tf.int64),
        'y': tf.io.FixedLenFeature([3], tf.int64),
    })
    # [1, 2, 3] + [1, 2, 3] - [2, 2, 2] - [-1, -3, 1] = [1, 5, 3]
    expected_data = [
        {'out': [1, 5, 3]}
    ]
    expected_metadata = tft_unit.metadata_from_feature_spec(
        {'out': tf.io.FixedLenFeature([3], tf.int64)})
    self.assertAnalyzeAndTransformResults(
        input_data, input_metadata, preprocessing_fn, expected_data,
        expected_metadata)

  @tft_unit.named_parameters(('NoDeepCopy', False), ('WithDeepCopy', True))
  def testMultipleLevelsOfAnalyzers(self, with_deep_copy):
    # Test a preprocessing function similar to scale_to_0_1 except that it
    # involves multiple interleavings of analyzers and transforms.
    def preprocessing_fn(inputs):
      scaled_to_0 = inputs['x'] - tft.min(inputs['x'])
      scaled_to_0_1 = scaled_to_0 / tft.max(scaled_to_0)
      return {'x_scaled': scaled_to_0_1}

    input_data = [{'x': 4}, {'x': 1}, {'x': 5}, {'x': 2}]
    input_metadata = tft_unit.metadata_from_feature_spec(
        {'x': tf.io.FixedLenFeature([], tf.float32)})
    expected_data = [
        {'x_scaled': 0.75},
        {'x_scaled': 0.0},
        {'x_scaled': 1.0},
        {'x_scaled': 0.25}
    ]
    expected_metadata = tft_unit.metadata_from_feature_spec(
        {'x_scaled': tf.io.FixedLenFeature([], tf.float32)})
    with beam_impl.Context(use_deep_copy_optimization=with_deep_copy):
      # NOTE: In order to correctly test deep_copy here, we can't pass test_data
      # to assertAnalyzeAndTransformResults.
      # Not passing test_data to assertAnalyzeAndTransformResults means that
      # tft.AnalyzeAndTransform is called, exercising the right code path.
      self.assertAnalyzeAndTransformResults(
          input_data, input_metadata, preprocessing_fn, expected_data,
          expected_metadata)

  def testRawFeedDictInput(self):
    # Test the ability to feed raw data into AnalyzeDataset and TransformDataset
    # by using subclasses of these transforms which create batches of size 1.
    def preprocessing_fn(inputs):
      sequence_example = inputs['sequence_example']

      # Ordinarily this would have shape (batch_size,) since 'sequence_example'
      # was defined as a FixedLenFeature with shape ().  But since we specified
      # desired_batch_size, we can assume that the shape is (1,), and reshape
      # to ().
      sequence_example = tf.reshape(sequence_example, ())

      # Parse the sequence example.
      feature_spec = {
          'x':
              tf.io.FixedLenSequenceFeature(
                  shape=[], dtype=tf.string, default_value=None)
      }
      _, sequences = tf.io.parse_single_sequence_example(
          sequence_example, sequence_features=feature_spec)

      # Create a batch based on the sequence "x".
      return {'x': sequences['x']}

    def text_sequence_example_to_binary(text_proto):
      proto = text_format.Merge(text_proto, example_pb2.SequenceExample())
      return proto.SerializeToString()

    sequence_examples = [
        """
        feature_lists: {
          feature_list: {
            key: "x"
            value: {
              feature: {bytes_list: {value: 'ab'}}
              feature: {bytes_list: {value: ''}}
              feature: {bytes_list: {value: 'c'}}
              feature: {bytes_list: {value: 'd'}}
            }
          }
        }
        """,
        """
        feature_lists: {
          feature_list: {
            key: "x"
            value: {
              feature: {bytes_list: {value: 'ef'}}
              feature: {bytes_list: {value: 'g'}}
            }
          }
        }
        """
    ]
    input_data = [
        {'sequence_example': text_sequence_example_to_binary(sequence_example)}
        for sequence_example in sequence_examples]
    input_metadata = tft_unit.metadata_from_feature_spec(
        {'sequence_example': tf.io.FixedLenFeature([], tf.string)})
    expected_data = [
        {'x': b'ab'},
        {'x': b''},
        {'x': b'c'},
        {'x': b'd'},
        {'x': b'ef'},
        {'x': b'g'}
    ]
    expected_metadata = tft_unit.metadata_from_feature_spec(
        {'x': tf.io.FixedLenFeature([], tf.string)})

    self.assertAnalyzeAndTransformResults(
        input_data, input_metadata, preprocessing_fn, expected_data,
        expected_metadata, desired_batch_size=1)

  def testAnalyzerBeforeMap(self):
    def preprocessing_fn(inputs):
      return {'x_scaled': tft.scale_to_0_1(inputs['x'])}

    input_data = [{'x': 4}, {'x': 1}, {'x': 5}, {'x': 2}]
    input_metadata = tft_unit.metadata_from_feature_spec(
        {'x': tf.io.FixedLenFeature([], tf.float32)})
    expected_data = [
        {'x_scaled': 0.75},
        {'x_scaled': 0.0},
        {'x_scaled': 1.0},
        {'x_scaled': 0.25}
    ]
    expected_metadata = tft_unit.metadata_from_feature_spec(
        {'x_scaled': tf.io.FixedLenFeature([], tf.float32)})
    self.assertAnalyzeAndTransformResults(
        input_data, input_metadata, preprocessing_fn, expected_data,
        expected_metadata)

  def testTransformWithExcludedOutputs(self):
    def preprocessing_fn(inputs):
      return {
          'x_scaled': tft.scale_to_0_1(inputs['x']),
          'y_scaled': tft.scale_to_0_1(inputs['y'])
      }

    # Run AnalyzeAndTransform on some input data and compare with expected
    # output.
    input_data = [{'x': 5, 'y': 1}, {'x': 1, 'y': 2}]
    input_metadata = tft_unit.metadata_from_feature_spec({
        'x': tf.io.FixedLenFeature([], tf.float32),
        'y': tf.io.FixedLenFeature([], tf.float32)
    })
    with beam_impl.Context(temp_dir=self.get_temp_dir()):
      transform_fn = (
          (input_data, input_metadata) | beam_impl.AnalyzeDataset(
              preprocessing_fn))

    # Take the transform function and use TransformDataset to apply it to
    # some eval data, with missing 'y' column.
    eval_data = [{'x': 6}]
    eval_metadata = tft_unit.metadata_from_feature_spec(
        {'x': tf.io.FixedLenFeature([], tf.float32)})
    transformed_eval_data, transformed_eval_metadata = (
        ((eval_data, eval_metadata), transform_fn)
        | beam_impl.TransformDataset(exclude_outputs=['y_scaled']))

    expected_transformed_eval_data = [{'x_scaled': 1.25}]
    expected_transformed_eval_metadata = tft_unit.metadata_from_feature_spec(
        {'x_scaled': tf.io.FixedLenFeature([], tf.float32)})
    self.assertDataCloseOrEqual(transformed_eval_data,
                                expected_transformed_eval_data)
    self.assertEqual(transformed_eval_metadata.dataset_metadata,
                     expected_transformed_eval_metadata)

  def testMapSparseColumns(self):
    # Define a transform that takes a sparse column and a varlen column, and
    # returns a combination of dense, sparse, and varlen columns.
    def preprocessing_fn(inputs):
      sparse_sum = tf.sparse.reduce_sum(inputs['sparse'], axis=1)
      sparse_sum.set_shape([None])
      return {
          'fixed': sparse_sum,  # Schema should be inferred.
          'varlen': inputs['varlen'],  # Schema should be inferred.
      }

    input_data = [
        {'idx': [0, 1], 'val': [0., 1.], 'varlen': [0., 1.]},
        {'idx': [2, 3], 'val': [2., 3.], 'varlen': [3., 4., 5.]},
        {'idx': [4, 5], 'val': [4., 5.], 'varlen': [6., 7.]}
    ]
    input_metadata = tft_unit.metadata_from_feature_spec({
        'sparse': tf.io.SparseFeature('idx', 'val', tf.float32, 10),
        'varlen': tf.io.VarLenFeature(tf.float32)
    })
    expected_data = [
        {'fixed': 1.0, 'varlen': [0., 1.]},
        {'fixed': 5.0, 'varlen': [3., 4., 5.]},
        {'fixed': 9.0, 'varlen': [6., 7.]}
    ]
    expected_metadata = tft_unit.metadata_from_feature_spec({
        'fixed': tf.io.FixedLenFeature([], tf.float32),
        'varlen': tf.io.VarLenFeature(tf.float32)
    })
    self.assertAnalyzeAndTransformResults(
        input_data, input_metadata, preprocessing_fn, expected_data,
        expected_metadata)

  def testSingleMap(self):
    def preprocessing_fn(inputs):
      return {'ab': tf.multiply(inputs['a'], inputs['b'])}

    input_data = [
        {'a': 4, 'b': 3},
        {'a': 1, 'b': 2},
        {'a': 5, 'b': 6},
        {'a': 2, 'b': 3}
    ]
    input_metadata = tft_unit.metadata_from_feature_spec({
        'a': tf.io.FixedLenFeature([], tf.float32),
        'b': tf.io.FixedLenFeature([], tf.float32)
    })
    expected_data = [
        {'ab': 12},
        {'ab': 2},
        {'ab': 30},
        {'ab': 6}
    ]
    expected_metadata = tft_unit.metadata_from_feature_spec(
        {'ab': tf.io.FixedLenFeature([], tf.float32)})
    self.assertAnalyzeAndTransformResults(
        input_data, input_metadata, preprocessing_fn, expected_data,
        expected_metadata)

  def testSingleMapWithOnlyAnalyzerInputs(self):
    def preprocessing_fn(inputs):
      ab = tf.multiply(inputs['a'], inputs['b'])
      return {'ab': ab, 'new_c': inputs['c'] + tft.mean(inputs['c'])}

    test_data = [
        {'a': 4, 'b': 3, 'c': 1},
        {'a': 1, 'b': 2, 'c': 2},
        {'a': 5, 'b': 6, 'c': 3},
        {'a': 2, 'b': 3, 'c': 4}
    ]
    input_data = [
        {'c': 10},
        {'c': 20}
    ]
    input_metadata = tft_unit.metadata_from_feature_spec({
        'a': tf.io.FixedLenFeature([], tf.float32),
        'b': tf.io.FixedLenFeature([], tf.float32),
        'c': tf.io.FixedLenFeature([], tf.float32),
        'd': tf.io.VarLenFeature(tf.int64)
    })
    expected_data = [
        {'ab': 12, 'new_c': 16},
        {'ab': 2, 'new_c': 17},
        {'ab': 30, 'new_c': 18},
        {'ab': 6, 'new_c': 19}
    ]
    expected_metadata = tft_unit.metadata_from_feature_spec({
        'ab': tf.io.FixedLenFeature([], tf.float32),
        'new_c': tf.io.FixedLenFeature([], tf.float32)
    })
    self.assertAnalyzeAndTransformResults(
        input_data, input_metadata, preprocessing_fn, expected_data,
        expected_metadata, test_data=test_data)

  def testMapWithCond(self):
    def preprocessing_fn(inputs):
      return {
          'a':
              tf.cond(
                  pred=tf.constant(True),
                  true_fn=lambda: inputs['a'],
                  false_fn=lambda: inputs['b'])
      }

    input_data = [
        {'a': 4, 'b': 3},
        {'a': 1, 'b': 2},
        {'a': 5, 'b': 6},
        {'a': 2, 'b': 3}
    ]
    input_metadata = tft_unit.metadata_from_feature_spec({
        'a': tf.io.FixedLenFeature([], tf.float32),
        'b': tf.io.FixedLenFeature([], tf.float32)
    })
    expected_data = [
        {'a': 4},
        {'a': 1},
        {'a': 5},
        {'a': 2}
    ]
    expected_metadata = tft_unit.metadata_from_feature_spec(
        {'a': tf.io.FixedLenFeature([], tf.float32)})
    self.assertAnalyzeAndTransformResults(
        input_data, input_metadata, preprocessing_fn, expected_data,
        expected_metadata)

  def testPyFuncs(self):
    def my_multiply(x, y):
      return x*y

    def my_add(x, y):
      return x+y

    def preprocessing_fn(inputs):
      result = {
          'a+b': tft.apply_pyfunc(
              my_add, tf.float32, True, 'add', inputs['a'], inputs['b']),
          'a+c': tft.apply_pyfunc(
              my_add, tf.float32, True, 'add', inputs['a'], inputs['c']),
          'ab': tft.apply_pyfunc(
              my_multiply, tf.float32, False, 'multiply',
              inputs['a'], inputs['b']),
          'sum_scaled': tft.scale_to_0_1(
              tft.apply_pyfunc(
                  my_add, tf.float32, True, 'add', inputs['a'], inputs['c']))
      }
      for value in result.values():
        value.set_shape([1,])
      return result

    input_data = [
        {'a': 4, 'b': 3, 'c': 2},
        {'a': 1, 'b': 2, 'c': 3},
        {'a': 5, 'b': 6, 'c': 7},
        {'a': 2, 'b': 3, 'c': 4}
    ]
    input_metadata = tft_unit.metadata_from_feature_spec({
        'a': tf.io.FixedLenFeature([], tf.float32),
        'b': tf.io.FixedLenFeature([], tf.float32),
        'c': tf.io.FixedLenFeature([], tf.float32)
    })
    expected_data = [
        {'ab': 12, 'a+b': 7, 'a+c': 6, 'sum_scaled': 0.25},
        {'ab': 2, 'a+b': 3, 'a+c': 4, 'sum_scaled': 0},
        {'ab': 30, 'a+b': 11, 'a+c': 12, 'sum_scaled': 1},
        {'ab': 6, 'a+b': 5, 'a+c': 6, 'sum_scaled': 0.25}
    ]
    # When calling tf.py_func, the output shape is set to unknown.
    expected_metadata = tft_unit.metadata_from_feature_spec({
        'ab': tf.io.FixedLenFeature([], tf.float32),
        'a+b': tf.io.FixedLenFeature([], tf.float32),
        'a+c': tf.io.FixedLenFeature([], tf.float32),
        'sum_scaled': tf.io.FixedLenFeature([], tf.float32)
    })
    self.assertAnalyzeAndTransformResults(
        input_data, input_metadata, preprocessing_fn, expected_data,
        expected_metadata)

  def testWithMoreThanDesiredBatchSize(self):
    def preprocessing_fn(inputs):
      return {
          'ab': tf.multiply(inputs['a'], inputs['b']),
          'i': tft.compute_and_apply_vocabulary(inputs['c'])
      }

    batch_size = 100
    num_instances = batch_size + 1
    input_data = [{
        'a': 2,
        'b': i,
        'c': '%.10i' % i,  # Front-padded to facilitate lexicographic sorting.
    } for i in range(num_instances)]
    input_metadata = tft_unit.metadata_from_feature_spec({
        'a': tf.io.FixedLenFeature([], tf.float32),
        'b': tf.io.FixedLenFeature([], tf.float32),
        'c': tf.io.FixedLenFeature([], tf.string)
    })
    expected_data = [{
        'ab': 2*i,
        'i': (len(input_data) - 1) - i,  # Due to reverse lexicographic sorting.
    } for i in range(len(input_data))]
    expected_metadata = tft_unit.metadata_from_feature_spec({
        'ab': tf.io.FixedLenFeature([], tf.float32),
        'i': tf.io.FixedLenFeature([], tf.int64),
    }, {
        'i':
            schema_pb2.IntDomain(
                min=-1, max=num_instances - 1, is_categorical=True)
    })
    self.assertAnalyzeAndTransformResults(
        input_data,
        input_metadata,
        preprocessing_fn,
        expected_data,
        expected_metadata,
        desired_batch_size=batch_size)

  def testWithUnicode(self):
    def preprocessing_fn(inputs):
      return {'a b': tf.strings.join([inputs['a'], inputs['b']], separator=' ')}

    input_data = [{'a': 'Hello', 'b': 'world'}, {'a': 'Hello', 'b': u'κόσμε'}]
    input_metadata = tft_unit.metadata_from_feature_spec({
        'a': tf.io.FixedLenFeature([], tf.string),
        'b': tf.io.FixedLenFeature([], tf.string),
    })
    expected_data = [
        {'a b': b'Hello world'},
        {'a b': u'Hello κόσμε'.encode('utf-8')}
    ]
    expected_metadata = tft_unit.metadata_from_feature_spec(
        {'a b': tf.io.FixedLenFeature([], tf.string)})
    self.assertAnalyzeAndTransformResults(
        input_data, input_metadata, preprocessing_fn, expected_data,
        expected_metadata)

  def testComposedMaps(self):
    def preprocessing_fn(inputs):
      return {
          'a(b+c)': tf.multiply(
              inputs['a'], tf.add(inputs['b'], inputs['c']))
      }

    input_data = [{'a': 4, 'b': 3, 'c': 3}, {'a': 1, 'b': 2, 'c': 1}]
    input_metadata = tft_unit.metadata_from_feature_spec({
        'a': tf.io.FixedLenFeature([], tf.float32),
        'b': tf.io.FixedLenFeature([], tf.float32),
        'c': tf.io.FixedLenFeature([], tf.float32)
    })
    expected_data = [{'a(b+c)': 24}, {'a(b+c)': 3}]
    expected_metadata = tft_unit.metadata_from_feature_spec(
        {'a(b+c)': tf.io.FixedLenFeature([], tf.float32)})
    self.assertAnalyzeAndTransformResults(
        input_data, input_metadata, preprocessing_fn, expected_data,
        expected_metadata)

  @tft_unit.parameters((True,), (False,))
  def testScaleUnitInterval(self, elementwise):

    def preprocessing_fn(inputs):
      outputs = {}
      cols = ('x', 'y')
      for col, scaled_t in zip(
          cols,
          tf.unstack(
              tft.scale_to_0_1(
                  tf.stack([inputs[col] for col in cols], axis=1),
                  elementwise=elementwise),
              axis=1)):
        outputs[col + '_scaled'] = scaled_t
      return outputs

    input_data = [{
        'x': 4,
        'y': 5
    }, {
        'x': 1,
        'y': 2
    }, {
        'x': 5,
        'y': 6
    }, {
        'x': 2,
        'y': 3
    }]
    input_metadata = tft_unit.metadata_from_feature_spec({
        'x': tf.io.FixedLenFeature([], tf.float32),
        'y': tf.io.FixedLenFeature([], tf.float32)
    })
    if elementwise:
      expected_data = [{
          'x_scaled': 0.75,
          'y_scaled': 0.75
      }, {
          'x_scaled': 0.0,
          'y_scaled': 0.0
      }, {
          'x_scaled': 1.0,
          'y_scaled': 1.0
      }, {
          'x_scaled': 0.25,
          'y_scaled': 0.25
      }]
    else:
      expected_data = [{
          'x_scaled': 0.6,
          'y_scaled': 0.8
      }, {
          'x_scaled': 0.0,
          'y_scaled': 0.2
      }, {
          'x_scaled': 0.8,
          'y_scaled': 1.0
      }, {
          'x_scaled': 0.2,
          'y_scaled': 0.4
      }]
    expected_metadata = tft_unit.metadata_from_feature_spec({
        'x_scaled': tf.io.FixedLenFeature([], tf.float32),
        'y_scaled': tf.io.FixedLenFeature([], tf.float32)
    })
    self.assertAnalyzeAndTransformResults(input_data, input_metadata,
                                          preprocessing_fn, expected_data,
                                          expected_metadata)

  @tft_unit.parameters((True,), (False,))
  def testScaleMinMax(self, elementwise):

    def preprocessing_fn(inputs):
      outputs = {}
      cols = ('x', 'y')
      for col, scaled_t in zip(
          cols,
          tf.unstack(
              tft.scale_by_min_max(
                  tf.stack([inputs[col] for col in cols], axis=1),
                  output_min=-1,
                  output_max=1,
                  elementwise=elementwise),
              axis=1)):
        outputs[col + '_scaled'] = scaled_t
      return outputs

    input_data = [{
        'x': 4,
        'y': 8
    }, {
        'x': 1,
        'y': 5
    }, {
        'x': 5,
        'y': 9
    }, {
        'x': 2,
        'y': 6
    }]
    input_metadata = tft_unit.metadata_from_feature_spec({
        'x': tf.io.FixedLenFeature([], tf.float32),
        'y': tf.io.FixedLenFeature([], tf.float32)
    })
    if elementwise:
      expected_data = [{
          'x_scaled': 0.5,
          'y_scaled': 0.5
      }, {
          'x_scaled': -1.0,
          'y_scaled': -1.0
      }, {
          'x_scaled': 1.0,
          'y_scaled': 1.0
      }, {
          'x_scaled': -0.5,
          'y_scaled': -0.5
      }]
    else:
      expected_data = [{
          'x_scaled': -0.25,
          'y_scaled': 0.75
      }, {
          'x_scaled': -1.0,
          'y_scaled': 0.0
      }, {
          'x_scaled': 0.0,
          'y_scaled': 1.0
      }, {
          'x_scaled': -0.75,
          'y_scaled': 0.25
      }]
    expected_metadata = tft_unit.metadata_from_feature_spec({
        'x_scaled': tf.io.FixedLenFeature([], tf.float32),
        'y_scaled': tf.io.FixedLenFeature([], tf.float32)
    })
    self.assertAnalyzeAndTransformResults(input_data, input_metadata,
                                          preprocessing_fn, expected_data,
                                          expected_metadata)

  def testScaleMinMaxConstant(self):

    def preprocessing_fn(inputs):
      return {'x_scaled': tft.scale_by_min_max(inputs['x'], 0, 10)}

    input_data = [{'x': 4}, {'x': 4}, {'x': 4}, {'x': 4}]
    input_metadata = tft_unit.metadata_from_feature_spec(
        {'x': tf.io.FixedLenFeature([], tf.float32)})
    expected_data = [{
        'x_scaled': 5
    }, {
        'x_scaled': 5
    }, {
        'x_scaled': 5
    }, {
        'x_scaled': 5
    }]
    expected_metadata = tft_unit.metadata_from_feature_spec(
        {'x_scaled': tf.io.FixedLenFeature([], tf.float32)})
    self.assertAnalyzeAndTransformResults(input_data, input_metadata,
                                          preprocessing_fn, expected_data,
                                          expected_metadata)

  def testScaleMinMaxConstantElementwise(self):

    def preprocessing_fn(inputs):
      outputs = {}
      cols = ('x', 'y')
      for col, scaled_t in zip(
          cols,
          tf.unstack(
              tft.scale_by_min_max(
                  tf.stack([inputs[col] for col in cols], axis=1),
                  output_min=0,
                  output_max=10,
                  elementwise=True),
              axis=1)):
        outputs[col + '_scaled'] = scaled_t
      return outputs

    input_data = [{
        'x': 4,
        'y': 1
    }, {
        'x': 4,
        'y': 1
    }, {
        'x': 4,
        'y': 2
    }, {
        'x': 4,
        'y': 2
    }]
    input_metadata = tft_unit.metadata_from_feature_spec({
        'x': tf.io.FixedLenFeature([], tf.float32),
        'y': tf.io.FixedLenFeature([], tf.float32)
    })
    expected_data = [{
        'x_scaled': 5,
        'y_scaled': 0
    }, {
        'x_scaled': 5,
        'y_scaled': 0
    }, {
        'x_scaled': 5,
        'y_scaled': 10
    }, {
        'x_scaled': 5,
        'y_scaled': 10
    }]
    expected_metadata = tft_unit.metadata_from_feature_spec({
        'x_scaled': tf.io.FixedLenFeature([], tf.float32),
        'y_scaled': tf.io.FixedLenFeature([], tf.float32)
    })
    self.assertAnalyzeAndTransformResults(input_data, input_metadata,
                                          preprocessing_fn, expected_data,
                                          expected_metadata)

  def testScaleMinMaxError(self):

    def preprocessing_fn(inputs):
      return {'x_scaled': tft.scale_by_min_max(inputs['x'], 2, 1)}

    input_data = [{'x': 1}]
    input_metadata = tft_unit.metadata_from_feature_spec(
        {'x': tf.io.FixedLenFeature([], tf.float32)})
    expected_data = [{'x_scaled': float('nan')}]
    expected_metadata = tft_unit.metadata_from_feature_spec(
        {'x_scaled': tf.io.FixedLenFeature([], tf.float32)})
    with self.assertRaises(ValueError) as context:
      self.assertAnalyzeAndTransformResults(input_data, input_metadata,
                                            preprocessing_fn, expected_data,
                                            expected_metadata)
    self.assertTrue(
        'output_min must be less than output_max' in str(context.exception))

  @tft_unit.parameters(*itertools.product([
      tf.int16,
      tf.int32,
      tf.int64,
      tf.float32,
      tf.float64,
  ], (True, False)))
  def testScaleToZScore(self, input_dtype, elementwise):
    def preprocessing_fn(inputs):

      def scale_to_z_score(tensor):
        z_score = tft.scale_to_z_score(
            tf.cast(tensor, input_dtype), elementwise=elementwise)
        self.assertEqual(z_score.dtype, _mean_output_dtype(input_dtype))
        return tf.cast(z_score, tf.float32)

      return {
          'x_scaled': scale_to_z_score(inputs['x']),
          'y_scaled': scale_to_z_score(inputs['y']),
          's_scaled': scale_to_z_score(inputs['s']),
      }

    if elementwise:
      input_data = [{
          'x': [-4., 4],
          'y': [0., 0],
          's': 3.,
      }, {
          'x': [10., -10.],
          'y': [0., 0],
          's': 4.,
      }, {
          'x': [2., -2.],
          'y': [0., 0],
          's': 4.,
      }, {
          'x': [4., -4.],
          'y': [0., 0],
          's': 5.,
      }]
      # Mean(x) = [3, -3], Mean(y) = [0, 0], Mean(s) = 4
      # Var(x) = (+-7^2 + 7^2 + +-1^2 + 1^2) / 4 = 25, Var(y) = 0, Var(s) = 0.5
      # StdDev(x) = 5, StdDev(y) = 0, StdDev(s) = sqrt(0.5)
      expected_data = [
          {
              'x_scaled': [-1.4, 1.4],  # [(-4 - 3) / 5, (10 - 3) / 5]
              'y_scaled': [0., 0.],
              's_scaled': -1. / 0.5**(0.5),  # (3 - 4) / sqrt(0.5)
          },
          {
              'x_scaled': [-.2, .2],  # [(2 - 3) / 5, (4 - 3) / 5]
              'y_scaled': [0., 0.],
              's_scaled': 0.,
          },
          {
              'x_scaled': [1.4, -1.4],  # [(4 + 3) / 5, (-10 + 3) / 5]
              'y_scaled': [0., 0.],
              's_scaled': 0.,
          },
          {
              'x_scaled': [.2, -.2],  # [(-2 + 3) / 5, (-4 + 3) / 5]
              'y_scaled': [0., 0.],
              's_scaled': 1. / 0.5**(0.5),  # (5 - 4) / sqrt(0.5)
          }
      ]
    else:
      input_data = [{
          'x': [-4., 2.],
          'y': [0., 0],
          's': 3.,
      }, {
          'x': [10., 4.],
          'y': [0., 0],
          's': 5.,
      }]
      # Mean(x) = 3, Mean(y) = 0, Mean(s) = 4
      # Var(x) = (-7^2 + -1^2 + 7^2 + 1^2) / 4 = 25, Var(y) = 0, Var(s) = 1
      # StdDev(x) = 5, StdDev(y) = 0, StdDev(s) = 1
      expected_data = [
          {
              'x_scaled': [-1.4, -.2],  # [(-4 - 3) / 5, (2 - 3) / 5]
              'y_scaled': [0., 0.],
              's_scaled': -1.,
          },
          {
              'x_scaled': [1.4, .2],  # [(10 - 3) / 5, (4 - 3) / 5]
              'y_scaled': [0., 0.],
              's_scaled': 1.,
          }
      ]
    input_metadata = tft_unit.metadata_from_feature_spec({
        'x': tf.io.FixedLenFeature([2], _canonical_dtype(input_dtype)),
        'y': tf.io.FixedLenFeature([2], _canonical_dtype(input_dtype)),
        's': tf.io.FixedLenFeature([], _canonical_dtype(input_dtype)),
    })
    expected_metadata = tft_unit.metadata_from_feature_spec({
        'x_scaled': tf.io.FixedLenFeature([2], tf.float32),
        'y_scaled': tf.io.FixedLenFeature([2], tf.float32),
        's_scaled': tf.io.FixedLenFeature([], tf.float32),
    })
    self.assertAnalyzeAndTransformResults(input_data, input_metadata,
                                          preprocessing_fn, expected_data,
                                          expected_metadata)

  @tft_unit.parameters(*itertools.product([
      tf.int16,
      tf.int32,
      tf.int64,
      tf.float32,
      tf.float64,
  ], (True, False)))
  def testScaleToZScoreSparse(self, input_dtype, elementwise):

    def preprocessing_fn(inputs):
      z_score = tf.sparse.to_dense(
          tft.scale_to_z_score(
              tf.cast(inputs['x'], input_dtype), elementwise=elementwise),
          default_value=np.nan)
      z_score.set_shape([None, 4])
      self.assertEqual(z_score.dtype, _mean_output_dtype(input_dtype))
      return {
          'x_scaled': tf.cast(z_score, tf.float32)
      }

    input_data = [
        {'idx': [0, 1], 'val': [-4, 10]},
        {'idx': [0, 1], 'val': [2, 4]},
    ]
    input_metadata = tft_unit.metadata_from_feature_spec({
        'x': tf.io.SparseFeature('idx', 'val', _canonical_dtype(input_dtype), 4)
    })
    if elementwise:
      # Mean(x) = [-1, 7]
      # Var(x) = [9, 9]
      # StdDev(x) = [3, 3]
      expected_data = [
          {
              'x_scaled': [-1., 1.,
                           float('nan'),
                           float('nan')]  # [(-4 +1 ) / 3, (10 -7) / 3]
          },
          {
              'x_scaled': [1., -1.,
                           float('nan'),
                           float('nan')]  # [(2 + 1) / 3, (4 - 7) / 3]
          }
      ]
    else:
      # Mean = 3
      # Var = 25
      # Std Dev = 5
      expected_data = [
          {
              'x_scaled': [-1.4, 1.4, float('nan'),
                           float('nan')]  # [(-4 - 3) / 5, (10 - 3) / 5]
          },
          {
              'x_scaled': [-.2, .2, float('nan'),
                           float('nan')]  # [(2 - 3) / 5, (4 - 3) / 5]
          }
      ]
    expected_metadata = tft_unit.metadata_from_feature_spec(
        {'x_scaled': tf.io.FixedLenFeature([4], tf.float32)})
    self.assertAnalyzeAndTransformResults(input_data, input_metadata,
                                          preprocessing_fn, expected_data,
                                          expected_metadata)

  @tft_unit.parameters(*itertools.product([
      tf.int16,
      tf.int32,
      tf.int64,
      tf.float32,
      tf.float64,
  ]))
  def testScaleToZScorePerSparseKey(self, input_dtype):
    # TODO(b/131852830) Add elementwise tests.
    def preprocessing_fn(inputs):

      def scale_to_z_score_per_key(tensor, key):
        z_score = tft.scale_to_z_score_per_key(
            tf.cast(tensor, input_dtype), key=key, elementwise=False)
        self.assertEqual(z_score.dtype, _mean_output_dtype(input_dtype))
        return tf.cast(z_score, tf.float32)

      return {
          'x_scaled': scale_to_z_score_per_key(inputs['x'], inputs['key']),
          'y_scaled': scale_to_z_score_per_key(inputs['y'], inputs['key']),
      }
    input_data = [{
        'x': [-4., 2.],
        'y': [0., 0],
        'key': ['a', 'a'],
    }, {
        'x': [10., 4.],
        'y': [0., 0],
        'key': ['a', 'a'],
    }, {
        'x': [1., -1.],
        'y': [0., 0],
        'key': ['b', 'b'],
    }]
    # Mean(x) = 3, Mean(y) = 0
    # Var(x) = (-7^2 + -1^2 + 7^2 + 1^2) / 4 = 25, Var(y) = 0
    # StdDev(x) = 5, StdDev(y) = 0
    # 'b':
    # Mean(x) = 0, Mean(y) = 0
    # Var(x) = 1, Var(y) = 0
    # StdDev(x) = 1, StdDev(y) = 0
    expected_data = [
        {
            'x_scaled': [-1.4, -.2],  # [(-4 - 3) / 5, (2 - 3) / 5]
            'y_scaled': [0., 0.],
        },
        {
            'x_scaled': [1.4, .2],  # [(10 - 3) / 5, (4 - 3) / 5]
            'y_scaled': [0., 0.],
        },
        {
            'x_scaled': [1., -1.],  # [(1 - 0) / 1, (-1 - 0) / 1]
            'y_scaled': [0., 0.],
        }
    ]

    input_metadata = tft_unit.metadata_from_feature_spec({
        'x': tf.io.VarLenFeature(_canonical_dtype(input_dtype)),
        'y': tf.io.VarLenFeature(_canonical_dtype(input_dtype)),
        'key': tf.io.VarLenFeature(tf.string),
    })
    expected_metadata = tft_unit.metadata_from_feature_spec({
        'x_scaled': tf.io.VarLenFeature(tf.float32),
        'y_scaled': tf.io.VarLenFeature(tf.float32),
    })
    self.assertAnalyzeAndTransformResults(input_data, input_metadata,
                                          preprocessing_fn, expected_data,
                                          expected_metadata)

  @tft_unit.parameters(*itertools.product([
      tf.int16,
      tf.int32,
      tf.int64,
      tf.float32,
      tf.float64,
  ]))
  def testScaleToZScorePerKey(self, input_dtype):
    # TODO(b/131852830) Add elementwise tests.
    def preprocessing_fn(inputs):

      def scale_to_z_score_per_key(tensor, key):
        z_score = tft.scale_to_z_score_per_key(
            tf.cast(tensor, input_dtype), key=key, elementwise=False)
        self.assertEqual(z_score.dtype, _mean_output_dtype(input_dtype))
        return tf.cast(z_score, tf.float32)

      return {
          'x_scaled': scale_to_z_score_per_key(inputs['x'], inputs['key']),
          'y_scaled': scale_to_z_score_per_key(inputs['y'], inputs['key']),
          's_scaled': scale_to_z_score_per_key(inputs['s'], inputs['key']),
      }
    input_data = [{
        'x': [-4.],
        'y': [0.],
        's': 3.,
        'key': 'a',
    }, {
        'x': [10.],
        'y': [0.],
        's': -3.,
        'key': 'a',
    }, {
        'x': [1.],
        'y': [0.],
        's': 3.,
        'key': 'b',
    }, {
        'x': [2.],
        'y': [0.],
        's': 3.,
        'key': 'a',
    }, {
        'x': [4.],
        'y': [0.],
        's': -3.,
        'key': 'a',
    }, {
        'x': [-1.],
        'y': [0.],
        's': -3.,
        'key': 'b',
    }]
    # 'a':
    # Mean(x) = 3, Mean(y) = 0
    # Var(x) = (-7^2 + -1^2 + 7^2 + 1^2) / 4 = 25, Var(y) = 0
    # StdDev(x) = 5, StdDev(y) = 0
    # 'b':
    # Mean(x) = 0, Mean(y) = 0
    # Var(x) = 1, Var(y) = 0
    # StdDev(x) = 1, StdDev(y) = 0
    expected_data = [
        {
            'x_scaled': [-1.4],  # [(-4 - 3) / 5, (2 - 3) / 5]
            'y_scaled': [0.],
            's_scaled': 1.,
        },
        {
            'x_scaled': [1.4],  # [(10 - 3) / 5, (4 - 3) / 5]
            'y_scaled': [0.],
            's_scaled': -1.,
        },
        {
            'x_scaled': [1.],  # [(1 - 0) / 1, (-1 - 0) / 1]
            'y_scaled': [0.],
            's_scaled': 1.,
        },
        {
            'x_scaled': [-.2],  # [(-4 - 3) / 5, (2 - 3) / 5]
            'y_scaled': [0.],
            's_scaled': 1.,
        },
        {
            'x_scaled': [.2],  # [(10 - 3) / 5, (4 - 3) / 5]
            'y_scaled': [0.],
            's_scaled': -1.,
        },
        {
            'x_scaled': [-1.],  # [(1 - 0) / 1, (-1 - 0) / 1]
            'y_scaled': [0.],
            's_scaled': -1.,
        }
    ]

    input_metadata = tft_unit.metadata_from_feature_spec({
        'x': tf.io.FixedLenFeature([1], _canonical_dtype(input_dtype)),
        'y': tf.io.FixedLenFeature([1], _canonical_dtype(input_dtype)),
        's': tf.io.FixedLenFeature([], _canonical_dtype(input_dtype)),
        'key': tf.io.FixedLenFeature([], tf.string),
    })
    expected_metadata = tft_unit.metadata_from_feature_spec({
        'x_scaled': tf.io.FixedLenFeature([1], tf.float32),
        'y_scaled': tf.io.FixedLenFeature([1], tf.float32),
        's_scaled': tf.io.FixedLenFeature([], tf.float32),
    })
    self.assertAnalyzeAndTransformResults(input_data, input_metadata,
                                          preprocessing_fn, expected_data,
                                          expected_metadata)

  @tft_unit.parameters(*itertools.product([
      tf.int16,
      tf.int32,
      tf.int64,
      tf.float32,
      tf.float64,
  ]))
  def testScaleToZScoreSparsePerKey(self, input_dtype):
    # TODO(b/131852830) Add elementwise tests.
    def preprocessing_fn(inputs):
      z_score = tf.sparse.to_dense(
          tft.scale_to_z_score_per_key(
              tf.cast(inputs['x'], input_dtype),
              inputs['key'],
              elementwise=False),
          default_value=np.nan)
      z_score.set_shape([None, 4])
      self.assertEqual(z_score.dtype, _mean_output_dtype(input_dtype))
      return {
          'x_scaled': tf.cast(z_score, tf.float32)
      }

    input_data = [
        {'idx': [0, 1], 'val': [-4, 10], 'key_idx': [0, 1], 'key': ['a', 'a']},
        {'idx': [0, 1], 'val': [2, 1], 'key_idx': [0, 1], 'key': ['a', 'b']},
        {'idx': [0, 1], 'val': [-1, 4], 'key_idx': [0, 1], 'key': ['b', 'a']},
    ]
    input_metadata = tft_unit.metadata_from_feature_spec({
        'key': tf.io.SparseFeature('key_idx', 'key', tf.string, 4),
        'x': tf.io.SparseFeature('idx', 'val', _canonical_dtype(input_dtype), 4)
    })
    # 'a':
    # Mean = 3
    # Var = 25
    # Std Dev = 5
    # 'b':
    # Mean = 0
    # Var = 1
    # Std Dev = 1
    expected_data = [
        {
            'x_scaled': [-1.4, 1.4, float('nan'),
                         float('nan')]  # [(-4 - 3) / 5, (10 - 3) / 5]
        },
        {
            'x_scaled': [-.2, 1., float('nan'),
                         float('nan')]  # [(2 - 3) / 5, (1 - 0) / 1]
        },
        {
            'x_scaled': [-1., .2,
                         float('nan'),
                         float('nan')]  # [(-1 - 0) / 1, (4 - 3) / 5]
        }
    ]
    expected_metadata = tft_unit.metadata_from_feature_spec(
        {'x_scaled': tf.io.FixedLenFeature([4], tf.float32)})
    self.assertAnalyzeAndTransformResults(input_data, input_metadata,
                                          preprocessing_fn, expected_data,
                                          expected_metadata)

  def testMeanAndVar(self):
    def analyzer_fn(inputs):
      mean, var = analyzers._mean_and_var(inputs['x'])
      return {
          'mean': mean,
          'var': var
      }

    # NOTE: We force 10 batches: data has 100 elements and we request a batch
    # size of 10.
    input_data = [{'x': [x]}
                  for x in range(1, 101)]
    input_metadata = tft_unit.metadata_from_feature_spec({
        'x': tf.io.FixedLenFeature([1], tf.int64)
    })
    # The expected data has 2 boundaries that divides the data into 3 buckets.
    expected_outputs = {
        'mean': np.float32(50.5),
        'var': np.float32(833.25)
    }
    self.assertAnalyzerOutputs(
        input_data,
        input_metadata,
        analyzer_fn,
        expected_outputs,
        desired_batch_size=10)

  def testMeanAndVarPerKey(self):
    def analyzer_fn(inputs):
      key_vocab, mean, var = analyzers._mean_and_var_per_key(
          inputs['x'], inputs['key'])
      return {
          'key_vocab': key_vocab,
          'mean': mean,
          'var': var
      }

    # NOTE: We force 10 batches: data has 100 elements and we request a batch
    # size of 10.
    input_data = [{'x': [x], 'key': 'a' if x < 50 else 'b'}
                  for x in range(1, 101)]
    input_metadata = tft_unit.metadata_from_feature_spec({
        'x': tf.io.FixedLenFeature([1], tf.int64),
        'key': tf.io.FixedLenFeature([], tf.string)
    })
    # The expected data has 2 boundaries that divides the data into 3 buckets.
    expected_outputs = {
        'key_vocab': np.array([b'a', b'b'], np.object),
        'mean': np.array([25, 75], np.float32),
        'var': np.array([200, 216.666666], np.float32)
    }
    self.assertAnalyzerOutputs(
        input_data,
        input_metadata,
        analyzer_fn,
        expected_outputs,
        desired_batch_size=10)

  @tft_unit.named_parameters(('Int64In', tf.int64, {
      'min': tf.int64,
      'max': tf.int64,
      'sum': tf.int64,
      'size': tf.int64,
      'mean': tf.float32,
      'var': tf.float32
  }), ('Int32In', tf.int32, {
      'min': tf.int32,
      'max': tf.int32,
      'sum': tf.int64,
      'size': tf.int64,
      'mean': tf.float32,
      'var': tf.float32
  }), ('Int16In', tf.int16, {
      'min': tf.int16,
      'max': tf.int16,
      'sum': tf.int64,
      'size': tf.int64,
      'mean': tf.float32,
      'var': tf.float32
  }), ('Float64In', tf.float64, {
      'min': tf.float64,
      'max': tf.float64,
      'sum': tf.float64,
      'size': tf.int64,
      'mean': tf.float64,
      'var': tf.float64
  }), ('Float32In', tf.float32, {
      'min': tf.float32,
      'max': tf.float32,
      'sum': tf.float32,
      'size': tf.int64,
      'mean': tf.float32,
      'var': tf.float32
  }), ('Float16In', tf.float16, {
      'min': tf.float16,
      'max': tf.float16,
      'sum': tf.float32,
      'size': tf.int64,
      'mean': tf.float16,
      'var': tf.float16
  }))
  def testNumericAnalyzersWithScalarInputs(self, input_dtype, output_dtypes):

    def analyzer_fn(inputs):
      a = tf.cast(inputs['a'], input_dtype)

      def assert_and_cast_dtype(tensor, out_dtype):
        self.assertEqual(tensor.dtype, out_dtype)
        return tf.cast(tensor, _canonical_dtype(out_dtype))

      return {
          'min': assert_and_cast_dtype(tft.min(a),
                                       output_dtypes['min']),
          'max': assert_and_cast_dtype(tft.max(a),
                                       output_dtypes['max']),
          'sum': assert_and_cast_dtype(tft.sum(a),
                                       output_dtypes['sum']),
          'size': assert_and_cast_dtype(tft.size(a),
                                        output_dtypes['size']),
          'mean': assert_and_cast_dtype(tft.mean(a),
                                        output_dtypes['mean']),
          'var': assert_and_cast_dtype(tft.var(a),
                                       output_dtypes['var']),
      }

    input_data = [{'a': 4}, {'a': 1}]
    input_metadata = tft_unit.metadata_from_feature_spec(
        {'a': tf.io.FixedLenFeature([], _canonical_dtype(input_dtype))})
    expected_outputs = {
        'min': np.array(
            1, _canonical_dtype(output_dtypes['min']).as_numpy_dtype),
        'max': np.array(
            4, _canonical_dtype(output_dtypes['max']).as_numpy_dtype),
        'sum': np.array(
            5, _canonical_dtype(output_dtypes['sum']).as_numpy_dtype),
        'size': np.array(
            2, _canonical_dtype(output_dtypes['size']).as_numpy_dtype),
        'mean': np.array(
            2.5, _canonical_dtype(output_dtypes['mean']).as_numpy_dtype),
        'var': np.array(
            2.25, _canonical_dtype(output_dtypes['var']).as_numpy_dtype),
    }

    self.assertAnalyzerOutputs(
        input_data, input_metadata, analyzer_fn, expected_outputs)

  @tft_unit.parameters(*itertools.product([
      tf.int16,
      tf.int32,
      tf.int64,
      tf.float32,
      tf.float64,
      tf.uint8,
      tf.uint16,
  ], (True, False)))
  def testNumericAnalyzersWithSparseInputs(self, input_dtype,
                                           reduce_instance_dims):

    def analyzer_fn(inputs):
      return {
          'min':
              tft.min(inputs['a'], reduce_instance_dims=reduce_instance_dims),
          'max':
              tft.max(inputs['a'], reduce_instance_dims=reduce_instance_dims),
          'sum':
              tft.sum(inputs['a'], reduce_instance_dims=reduce_instance_dims),
          'size':
              tft.size(inputs['a'], reduce_instance_dims=reduce_instance_dims),
          'mean':
              tft.mean(inputs['a'], reduce_instance_dims=reduce_instance_dims),
          'var':
              tft.var(inputs['a'], reduce_instance_dims=reduce_instance_dims),
      }

    output_dtype = _canonical_dtype(input_dtype).as_numpy_dtype
    input_data = [
        {'idx': [0, 1], 'val': [0., 1.]},
        {'idx': [1, 3], 'val': [2., 3.]},
    ]
    input_metadata = tft_unit.metadata_from_feature_spec({
        'a': tf.io.SparseFeature('idx', 'val', _canonical_dtype(input_dtype), 4)
    })
    if reduce_instance_dims:
      expected_outputs = {
          'min': np.array(0., output_dtype),
          'max': np.array(3., output_dtype),
          'sum': np.array(6., output_dtype),
          'size': np.array(4, np.int64),
          'mean': np.array(1.5, np.float32),
          'var': np.array(1.25, np.float32),
      }
    else:
      if input_dtype.is_floating:
        missing_value_max = float('nan')
        missing_value_min = float('nan')
      else:
        missing_value_max = np.iinfo(output_dtype).min
        missing_value_min = np.iinfo(output_dtype).max
      expected_outputs = {
          'min': np.array([0., 1., missing_value_min, 3.], output_dtype),
          'max': np.array([0., 2., missing_value_max, 3.], output_dtype),
          'sum': np.array([0., 3., 0., 3.], output_dtype),
          'size': np.array([1, 2, 0, 1], np.int64),
          'mean': np.array([0., 1.5, float('nan'), 3.], np.float32),
          'var': np.array([0., 0.25, float('nan'), 0.], np.float32),
      }
    self.assertAnalyzerOutputs(input_data, input_metadata, analyzer_fn,
                               expected_outputs)

  def testNumericAnalyzersWithInputsAndAxis(self):
    def analyzer_fn(inputs):
      return {
          'min': tft.min(inputs['a'], reduce_instance_dims=False),
          'max': tft.max(inputs['a'], reduce_instance_dims=False),
          'sum': tft.sum(inputs['a'], reduce_instance_dims=False),
          'size': tft.size(inputs['a'], reduce_instance_dims=False),
          'mean': tft.mean(inputs['a'], reduce_instance_dims=False),
          'var': tft.var(inputs['a'], reduce_instance_dims=False),
      }

    input_data = [
        {'a': [8, 9, 3, 4]},
        {'a': [1, 2, 10, 11]}
    ]
    input_metadata = tft_unit.metadata_from_feature_spec(
        {'a': tf.io.FixedLenFeature([4], tf.int64)})
    expected_outputs = {
        'min': np.array([1, 2, 3, 4], np.int64),
        'max': np.array([8, 9, 10, 11], np.int64),
        'sum': np.array([9, 11, 13, 15], np.int64),
        'size': np.array([2, 2, 2, 2], np.int64),
        'mean': np.array([4.5, 5.5, 6.5, 7.5], np.float32),
        'var': np.array([12.25, 12.25, 12.25, 12.25], np.float32),
    }
    self.assertAnalyzerOutputs(
        input_data, input_metadata, analyzer_fn, expected_outputs)

  def testNumericAnalyzersWithNDInputsAndAxis(self):
    def analyzer_fn(inputs):
      return {
          'min': tft.min(inputs['a'], reduce_instance_dims=False),
          'max': tft.max(inputs['a'], reduce_instance_dims=False),
          'sum': tft.sum(inputs['a'], reduce_instance_dims=False),
          'size': tft.size(inputs['a'], reduce_instance_dims=False),
          'mean': tft.mean(inputs['a'], reduce_instance_dims=False),
          'var': tft.var(inputs['a'], reduce_instance_dims=False),
      }

    input_data = [
        {'a': [[8, 9], [3, 4]]},
        {'a': [[1, 2], [10, 11]]}]
    input_metadata = tft_unit.metadata_from_feature_spec(
        {'a': tf.io.FixedLenFeature([2, 2], tf.int64)})
    expected_outputs = {
        'min': np.array([[1, 2], [3, 4]], np.int64),
        'max': np.array([[8, 9], [10, 11]], np.int64),
        'sum': np.array([[9, 11], [13, 15]], np.int64),
        'size': np.array([[2, 2], [2, 2]], np.int64),
        'mean': np.array([[4.5, 5.5], [6.5, 7.5]], np.float32),
        'var': np.array([[12.25, 12.25], [12.25, 12.25]], np.float32),
    }
    self.assertAnalyzerOutputs(
        input_data, input_metadata, analyzer_fn, expected_outputs)

  def testNumericAnalyzersWithShape1NDInputsAndAxis(self):

    def analyzer_fn(inputs):
      return {
          'min': tft.min(inputs['a'], reduce_instance_dims=False),
          'max': tft.max(inputs['a'], reduce_instance_dims=False),
          'sum': tft.sum(inputs['a'], reduce_instance_dims=False),
          'size': tft.size(inputs['a'], reduce_instance_dims=False),
          'mean': tft.mean(inputs['a'], reduce_instance_dims=False),
          'var': tft.var(inputs['a'], reduce_instance_dims=False),
      }

    input_data = [{'a': [[8, 9]]}, {'a': [[1, 2]]}]
    input_metadata = tft_unit.metadata_from_feature_spec(
        {'a': tf.io.FixedLenFeature([1, 2], tf.int64)})
    expected_outputs = {
        'min': np.array([[1, 2]], np.int64),
        'max': np.array([[8, 9]], np.int64),
        'sum': np.array([[9, 11]], np.int64),
        'size': np.array([[2, 2]], np.int64),
        'mean': np.array([[4.5, 5.5]], np.float32),
        'var': np.array([[12.25, 12.25]], np.float32),
    }
    self.assertAnalyzerOutputs(input_data, input_metadata, analyzer_fn,
                               expected_outputs)

  def testNumericAnalyzersWithNDInputs(self):
    def analyzer_fn(inputs):
      return {
          'min': tft.min(inputs['a']),
          'max': tft.max(inputs['a']),
          'sum': tft.sum(inputs['a']),
          'size': tft.size(inputs['a']),
          'mean': tft.mean(inputs['a']),
          'var': tft.var(inputs['a']),
      }

    input_data = [
        {'a': [[4, 5], [6, 7]]},
        {'a': [[1, 2], [3, 4]]}
    ]
    input_metadata = tft_unit.metadata_from_feature_spec(
        {'a': tf.io.FixedLenFeature([2, 2], tf.int64)})
    expected_outputs = {
        'min': np.array(1, np.int64),
        'max': np.array(7, np.int64),
        'sum': np.array(32, np.int64),
        'size': np.array(8, np.int64),
        'mean': np.array(4.0, np.float32),
        'var': np.array(3.5, np.float32),
    }
    self.assertAnalyzerOutputs(
        input_data, input_metadata, analyzer_fn, expected_outputs)

  def testNumericMeanWithSparseTensorReduceFalseOverflow(self):

    def analyzer_fn(inputs):
      return {'mean': tft.mean(tf.cast(inputs['sparse'], tf.int32), False)}

    input_data = [
        {'idx': [0, 1], 'val': [1, 1]},
        {'idx': [1, 3], 'val': [2147483647, 3]},
    ]
    input_metadata = tft_unit.metadata_from_feature_spec(
        {'sparse': tf.io.SparseFeature('idx', 'val', tf.int64, 4)})
    expected_outputs = {
        'mean': np.array([1., 1073741824., float('nan'), 3.], np.float32)
    }
    self.assertAnalyzerOutputs(input_data, input_metadata, analyzer_fn,
                               expected_outputs)

  def testStringToTFIDF(self):
    def preprocessing_fn(inputs):
      inputs_as_ints = tft.compute_and_apply_vocabulary(
          tf.strings.split(inputs['a']))
      out_index, out_values = tft.tfidf(inputs_as_ints, 6)
      return {
          'tf_idf': out_values,
          'index': out_index
      }
    input_data = [{'a': 'hello hello world'},
                  {'a': 'hello goodbye hello world'},
                  {'a': 'I like pie pie pie'}]
    input_metadata = tft_unit.metadata_from_feature_spec(
        {'a': tf.io.FixedLenFeature([], tf.string)})

    # IDFs
    # hello = log(4/3) = 0.28768
    # world = log(4/3)
    # goodbye = log(4/2) = 0.69314
    # I = log(4/2)
    # like = log(4/2)
    # pie = log(4/2)
    log_4_over_2 = 1.69314718056
    log_4_over_3 = 1.28768207245
    expected_transformed_data = [{
        'tf_idf': [(2/3)*log_4_over_3, (1/3)*log_4_over_3],
        'index': [0, 2]
    }, {
        'tf_idf': [(2/4)*log_4_over_3, (1/4)*log_4_over_3, (1/4)*log_4_over_2],
        'index': [0, 2, 4]
    }, {
        'tf_idf': [(3/5)*log_4_over_2, (1/5)*log_4_over_2, (1/5)*log_4_over_2],
        'index': [1, 3, 5]
    }]
    expected_metadata = tft_unit.metadata_from_feature_spec({
        'tf_idf': tf.io.VarLenFeature(tf.float32),
        'index': tf.io.VarLenFeature(tf.int64)
    })
    self.assertAnalyzeAndTransformResults(
        input_data, input_metadata, preprocessing_fn, expected_transformed_data,
        expected_metadata)

  def testTFIDFNoData(self):
    def preprocessing_fn(inputs):
      inputs_as_ints = tft.compute_and_apply_vocabulary(
          tf.strings.split(inputs['a']))
      out_index, out_values = tft.tfidf(inputs_as_ints, 6)
      return {
          'tf_idf': out_values,
          'index': out_index
      }
    input_data = [{'a': ''}]
    input_metadata = tft_unit.metadata_from_feature_spec(
        {'a': tf.io.FixedLenFeature([], tf.string)})
    expected_transformed_data = [{'tf_idf': [], 'index': []}]
    expected_metadata = tft_unit.metadata_from_feature_spec({
        'tf_idf': tf.io.VarLenFeature(tf.float32),
        'index': tf.io.VarLenFeature(tf.int64)
    })
    self.assertAnalyzeAndTransformResults(
        input_data, input_metadata, preprocessing_fn, expected_transformed_data,
        expected_metadata)

  def testStringToTFIDFEmptyDoc(self):
    def preprocessing_fn(inputs):
      inputs_as_ints = tft.compute_and_apply_vocabulary(
          tf.strings.split(inputs['a']))
      out_index, out_values = tft.tfidf(inputs_as_ints, 6)
      return {
          'tf_idf': out_values,
          'index': out_index
      }
    input_data = [{'a': 'hello hello world'},
                  {'a': ''},
                  {'a': 'hello goodbye hello world'},
                  {'a': 'I like pie pie pie'}]
    input_metadata = tft_unit.metadata_from_feature_spec(
        {'a': tf.io.FixedLenFeature([], tf.string)})

    log_5_over_2 = 1.91629073187
    log_5_over_3 = 1.51082562376
    expected_transformed_data = [{
        'tf_idf': [(2/3)*log_5_over_3, (1/3)*log_5_over_3],
        'index': [0, 2]
    }, {
        'tf_idf': [],
        'index': []
    }, {
        'tf_idf': [(2/4)*log_5_over_3, (1/4)*log_5_over_3, (1/4)*log_5_over_2],
        'index': [0, 2, 4]
    }, {
        'tf_idf': [(3/5)*log_5_over_2, (1/5)*log_5_over_2, (1/5)*log_5_over_2],
        'index': [1, 3, 5]
    }]
    expected_metadata = tft_unit.metadata_from_feature_spec({
        'tf_idf': tf.io.VarLenFeature(tf.float32),
        'index': tf.io.VarLenFeature(tf.int64)
    })
    self.assertAnalyzeAndTransformResults(
        input_data, input_metadata, preprocessing_fn, expected_transformed_data,
        expected_metadata)

  def testIntToTFIDF(self):
    def preprocessing_fn(inputs):
      out_index, out_values = tft.tfidf(inputs['a'], 13)
      return {'tf_idf': out_values, 'index': out_index}
    input_data = [{'a': [2, 2, 0]},
                  {'a': [2, 6, 2, 0]},
                  {'a': [8, 10, 12, 12, 12]},
                 ]
    input_metadata = tft_unit.metadata_from_feature_spec(
        {'a': tf.io.VarLenFeature(tf.int64)})
    log_4_over_2 = 1.69314718056
    log_4_over_3 = 1.28768207245
    expected_data = [{
        'tf_idf': [(1/3)*log_4_over_3, (2/3)*log_4_over_3],
        'index': [0, 2]
    }, {
        'tf_idf': [(1/4)*log_4_over_3, (2/4)*log_4_over_3, (1/4)*log_4_over_2],
        'index': [0, 2, 6]
    }, {
        'tf_idf': [(1/5)*log_4_over_2, (1/5)*log_4_over_2, (3/5)*log_4_over_2],
        'index': [8, 10, 12]
    }]
    expected_schema = tft_unit.metadata_from_feature_spec({
        'tf_idf': tf.io.VarLenFeature(tf.float32),
        'index': tf.io.VarLenFeature(tf.int64)
    })
    self.assertAnalyzeAndTransformResults(
        input_data, input_metadata, preprocessing_fn, expected_data,
        expected_schema)

  def testIntToTFIDFWithoutSmoothing(self):
    def preprocessing_fn(inputs):
      out_index, out_values = tft.tfidf(inputs['a'], 13, smooth=False)
      return {'tf_idf': out_values, 'index': out_index}
    input_data = [{'a': [2, 2, 0]},
                  {'a': [2, 6, 2, 0]},
                  {'a': [8, 10, 12, 12, 12]},
                 ]
    input_metadata = tft_unit.metadata_from_feature_spec(
        {'a': tf.io.VarLenFeature(tf.int64)})
    log_3_over_2 = 1.4054651081
    log_3 = 2.0986122886
    expected_data = [{
        'tf_idf': [(1/3)*log_3_over_2, (2/3)*log_3_over_2],
        'index': [0, 2]
    }, {
        'tf_idf': [(1/4)*log_3_over_2, (2/4)*log_3_over_2, (1/4)*log_3],
        'index': [0, 2, 6]
    }, {
        'tf_idf': [(1/5)*log_3, (1/5)*log_3, (3/5)*log_3],
        'index': [8, 10, 12]
    }]
    expected_schema = tft_unit.metadata_from_feature_spec({
        'tf_idf': tf.io.VarLenFeature(tf.float32),
        'index': tf.io.VarLenFeature(tf.int64)
    })
    self.assertAnalyzeAndTransformResults(
        input_data, input_metadata, preprocessing_fn, expected_data,
        expected_schema)

  def testTFIDFWithOOV(self):
    test_vocab_size = 3
    def preprocessing_fn(inputs):
      inputs_as_ints = tft.compute_and_apply_vocabulary(
          tf.strings.split(inputs['a']), top_k=test_vocab_size)
      out_index, out_values = tft.tfidf(inputs_as_ints,
                                        test_vocab_size+1)
      return {
          'tf_idf': out_values,
          'index': out_index
      }
    input_data = [{'a': 'hello hello world'},
                  {'a': 'hello goodbye hello world'},
                  {'a': 'I like pie pie pie'}]
    input_metadata = tft_unit.metadata_from_feature_spec(
        {'a': tf.io.FixedLenFeature([], tf.string)})

    # IDFs
    # hello = log(3/3) = 0
    # pie = log(3/2) = 0.4054651081
    # world = log(3/3) = 0
    # OOV - goodbye, I, like = log(3/3)
    log_4_over_2 = 1.69314718056
    log_4_over_3 = 1.28768207245
    expected_transformed_data = [{
        'tf_idf': [(2/3)*log_4_over_3, (1/3)*log_4_over_3],
        'index': [0, 2]
    }, {
        'tf_idf': [(2/4)*log_4_over_3, (1/4)*log_4_over_3, (1/4)*log_4_over_3],
        'index': [0, 2, 3]
    }, {
        'tf_idf': [(3/5)*log_4_over_2, (2/5)*log_4_over_3],
        'index': [1, 3]
    }]
    expected_metadata = tft_unit.metadata_from_feature_spec({
        'tf_idf': tf.io.VarLenFeature(tf.float32),
        'index': tf.io.VarLenFeature(tf.int64)
    })
    self.assertAnalyzeAndTransformResults(
        input_data, input_metadata, preprocessing_fn, expected_transformed_data,
        expected_metadata)

  def testTFIDFWithNegatives(self):
    def preprocessing_fn(inputs):
      out_index, out_values = tft.tfidf(inputs['a'], 14)
      return {
          'tf_idf': out_values,
          'index': out_index
      }
    input_data = [{'a': [2, 2, -4]},
                  {'a': [2, 6, 2, -1]},
                  {'a': [8, 10, 12, 12, 12]},
                 ]
    input_metadata = tft_unit.metadata_from_feature_spec(
        {'a': tf.io.VarLenFeature(tf.int64)})

    log_4_over_2 = 1.69314718056
    log_4_over_3 = 1.28768207245
    # NOTE: -4 mod 14 = 10
    expected_transformed_data = [{
        'tf_idf': [(2/3)*log_4_over_3, (1/3)*log_4_over_3],
        'index': [2, 10]
    }, {
        'tf_idf': [(2/4)*log_4_over_3, (1/4)*log_4_over_2, (1/4)*log_4_over_2],
        'index': [2, 6, 13]
    }, {
        'tf_idf': [(1/5)*log_4_over_2, (1/5)*log_4_over_3, (3/5)*log_4_over_2],
        'index': [8, 10, 12]
    }]
    expected_metadata = tft_unit.metadata_from_feature_spec({
        'tf_idf': tf.io.VarLenFeature(tf.float32),
        'index': tf.io.VarLenFeature(tf.int64)
    })
    self.assertAnalyzeAndTransformResults(
        input_data, input_metadata, preprocessing_fn, expected_transformed_data,
        expected_metadata)

  @tft_unit.named_parameters(
      dict(
          testcase_name='string_feature_k2',
          feature_label_pairs=[
              (b'hello', 1),
              (b'hello', 1),
              (b'hello', 1),
              (b'goodbye', 1),
              (b'aaaaa', 1),
              (b'aaaaa', 1),
              (b'goodbye', 0),
              (b'goodbye', 0),
              (b'aaaaa', 1),
              (b'aaaaa', 1),
              (b'goodbye', 1),
              (b'goodbye', 0),
          ],
          feature_dtype=tf.string,
          expected_data=[-1, -1, -1, 0, 1, 1, 0, 0, 0, 1, 1, 0],
          k=2,
      ),
      dict(
          testcase_name='string_feature_k1',
          feature_label_pairs=[
              (b'hello', 1),
              (b'hello', 1),
              (b'hello', 1),
              (b'goodbye', 1),
              (b'aaaaa', 1),
              (b'aaaaa', 1),
              (b'goodbye', 0),
              (b'goodbye', 0),
              (b'aaaaa', 1),
              (b'aaaaa', 1),
              (b'goodbye', 1),
              (b'goodbye', 0),
          ],
          feature_dtype=tf.string,
          expected_data=[-1, -1, -1, 0, -1, -1, 0, 0, 0, -1, -1, 0],
          k=1,
      ),
      dict(
          testcase_name='int64_feature_k2',
          feature_label_pairs=[
              (3, 1),
              (3, 1),
              (3, 1),
              (1, 1),
              (2, 1),
              (2, 1),
              (1, 0),
              (1, 0),
              (2, 1),
              (2, 1),
              (1, 1),
              (1, 0),
          ],
          feature_dtype=tf.int64,
          expected_data=[-1, -1, -1, 0, 1, 1, 0, 0, 0, 1, 1, 0],
          k=2,
      ))
  def testVocabularyAnalyzerWithLabelsAndTopK(self,
                                              feature_label_pairs,
                                              feature_dtype,
                                              expected_data,
                                              k=2):
    input_data = []
    for feature, label in feature_label_pairs:
      input_data.append({'a': feature, 'label': label})
    input_metadata = tft_unit.metadata_from_feature_spec({
        'a': tf.io.FixedLenFeature([], feature_dtype),
        'label': tf.io.FixedLenFeature([], tf.int64)
    })
    expected_metadata = tft_unit.metadata_from_feature_spec({
        'index': tf.io.FixedLenFeature([], tf.int64),
    }, {'index': schema_pb2.IntDomain(min=-1, max=k - 1, is_categorical=True)})

    def preprocessing_fn(inputs):
      return {
          'index':
              tft.compute_and_apply_vocabulary(
                  inputs['a'], labels=inputs['label'], top_k=k)
      }

    expected_data = [{'index': val} for val in expected_data]

    self.assertAnalyzeAndTransformResults(input_data, input_metadata,
                                          preprocessing_fn, expected_data,
                                          expected_metadata)

  @tft_unit.named_parameters(
      dict(
          testcase_name='string_feature',
          features=[
              b'hello',
              b'hello',
              b'hello',
              b'goodbye',
              b'aaaaa',
              b'aaaaa',
              b'goodbye',
              b'goodbye',
              b'aaaaa',
              b'aaaaa',
              b'goodbye',
              b'goodbye',
          ],
          feature_dtype=tf.string,
          expected_tokens=[b'goodbye', b'aaaaa', b'hello'],
      ),
      dict(
          testcase_name='int64_feature',
          features=[
              3,
              3,
              3,
              1,
              2,
              2,
              1,
              1,
              2,
              2,
              1,
              1,
          ],
          feature_dtype=tf.int64,
          expected_tokens=[1, 2, 3]),
  )
  def testVocabularyAnalyzerStringVsIntegerFeature(
      self, features, feature_dtype, expected_tokens):
    """Ensure string and integer features are treated equivalently."""
    labels = [1, 1, 1, 1, 1, 1, 0, 0, 1, 1, 1, 0]
    input_data = []
    for feature, label in zip(features, labels):
      input_data.append({'a': feature, 'label': label})
    input_metadata = tft_unit.metadata_from_feature_spec({
        'a': tf.io.FixedLenFeature([], feature_dtype),
        'label': tf.io.FixedLenFeature([], tf.int64)
    })
    expected_metadata = input_metadata
    expected_mi = [1.975322, 1.6600708, 1.2450531]
    if feature_dtype != tf.string:
      expected_tokens = [str(t).encode() for t in expected_tokens]
    expected_vocab = list(zip(expected_tokens, expected_mi))

    def preprocessing_fn(inputs):
      tft.vocabulary(
          inputs['a'],
          labels=inputs['label'],
          store_frequency=True,
          vocab_filename='my_vocab')
      return inputs

    expected_data = input_data
    expected_vocab_file_contents = {'my_vocab': expected_vocab}

    self.assertAnalyzeAndTransformResults(
        input_data,
        input_metadata,
        preprocessing_fn,
        expected_data,
        expected_metadata,
        expected_vocab_file_contents=expected_vocab_file_contents)

  @tft_unit.named_parameters(
      dict(
          testcase_name='unadjusted_mi_binary_label',
          feature_label_pairs=[
              (b'informative', 1),
              (b'informative', 1),
              (b'informative', 1),
              (b'uninformative', 0),
              (b'uninformative', 1),
              (b'uninformative', 1),
              (b'uninformative', 0),
              (b'uninformative_rare', 0),
              (b'uninformative_rare', 1),
          ],
          expected_vocab=[
              (b'informative', 1.7548264),
              (b'uninformative', 0.33985),
              (b'uninformative_rare', 0.169925),
          ],
          use_adjusted_mutual_info=False),
      dict(
          testcase_name='unadjusted_mi_multi_class_label',
          feature_label_pairs=[
              (b'good_predictor_of_0', 0),
              (b'good_predictor_of_0', 0),
              (b'good_predictor_of_0', 0),
              (b'good_predictor_of_1', 1),
              (b'good_predictor_of_2', 2),
              (b'good_predictor_of_2', 2),
              (b'good_predictor_of_2', 2),
              (b'good_predictor_of_1', 1),
              (b'good_predictor_of_1', 1),
              (b'weak_predictor_of_1', 1),
              (b'good_predictor_of_0', 0),
              (b'good_predictor_of_1', 1),
              (b'good_predictor_of_1', 1),
              (b'good_predictor_of_1', 1),
              (b'weak_predictor_of_1', 0),
          ],
          expected_vocab=[
              (b'good_predictor_of_2', 6.9656615),
              (b'good_predictor_of_1', 6.5969831),
              (b'good_predictor_of_0', 6.3396921),
              (b'weak_predictor_of_1', 0.684463),
          ],
          use_adjusted_mutual_info=False),
      dict(
          testcase_name='unadjusted_mi_binary_label_with_weights',
          feature_label_pairs=[
              (b'informative_1', 1),
              (b'informative_1', 1),
              (b'informative_0', 0),
              (b'informative_0', 0),
              (b'uninformative', 0),
              (b'uninformative', 1),
              (b'informative_by_weight', 0),
              (b'informative_by_weight', 1),
          ],
          # uninformative and informative_by_weight have the same co-occurrence
          # relationship with the label but will have different importance
          # values due to the weighting.
          expected_vocab=[
              (b'informative_0', 0.316988),
              (b'informative_1', 0.1169884),
              (b'informative_by_weight', 0.060964),
              (b'uninformative', 0.0169925),
          ],
          weights=[.1, .1, .1, .1, .1, .1, .1, .5],
          use_adjusted_mutual_info=False),
      dict(
          testcase_name='unadjusted_mi_binary_label_min_diff_from_avg',
          feature_label_pairs=[
              (b'hello', 1),
              (b'hello', 1),
              (b'hello', 1),
              (b'goodbye', 1),
              (b'aaaaa', 1),
              (b'aaaaa', 1),
              (b'goodbye', 0),
              (b'goodbye', 0),
              (b'aaaaa', 1),
              (b'aaaaa', 1),
              (b'goodbye', 1),
              (b'goodbye', 0),
          ],
          # All features are weak predictors, so all are adjusted to zero.
          expected_vocab=[
              (b'hello', 0.0),
              (b'goodbye', 0.0),
              (b'aaaaa', 0.0),
          ],
          use_adjusted_mutual_info=False,
          min_diff_from_avg=2),
      dict(
          testcase_name='adjusted_mi_binary_label',
          feature_label_pairs=[
              (b'hello', 1),
              (b'hello', 1),
              (b'hello', 1),
              (b'goodbye', 1),
              (b'aaaaa', 1),
              (b'aaaaa', 1),
              (b'goodbye', 0),
              (b'goodbye', 0),
              (b'aaaaa', 1),
              (b'aaaaa', 1),
              (b'goodbye', 1),
              (b'goodbye', 0),
          ],
          expected_vocab=[
              (b'goodbye', 1.4070791),
              (b'aaaaa', 0.9987449),
              (b'hello', 0.5017179),
          ],
          use_adjusted_mutual_info=True),
      dict(
          testcase_name='adjusted_mi_binary_label_int64_feature',
          feature_label_pairs=[
              (3, 1),
              (3, 1),
              (3, 1),
              (1, 1),
              (2, 1),
              (2, 1),
              (1, 0),
              (1, 0),
              (2, 1),
              (2, 1),
              (1, 1),
              (1, 0),
          ],
          expected_vocab=[
              (b'1', 1.4070791),
              (b'2', 0.9987449),
              (b'3', 0.5017179),
          ],
          feature_dtype=tf.int64,
          use_adjusted_mutual_info=True),
      dict(
          testcase_name='adjusted_mi_multi_class_label',
          feature_label_pairs=[
              (b'good_predictor_of_0', 0),
              (b'good_predictor_of_0', 0),
              (b'good_predictor_of_0', 0),
              (b'good_predictor_of_1', 1),
              (b'good_predictor_of_2', 2),
              (b'good_predictor_of_2', 2),
              (b'good_predictor_of_2', 2),
              (b'good_predictor_of_1', 1),
              (b'good_predictor_of_1', 1),
              (b'weak_predictor_of_1', 1),
              (b'good_predictor_of_0', 0),
              (b'good_predictor_of_1', 1),
              (b'good_predictor_of_1', 1),
              (b'good_predictor_of_1', 1),
              (b'weak_predictor_of_1', 0),
          ],
          expected_vocab=[
              (b'good_predictor_of_1', 5.4800903),
              (b'good_predictor_of_2', 5.386102),
              (b'good_predictor_of_0', 4.9054723),
              (b'weak_predictor_of_1', -0.9748023),
          ],
          use_adjusted_mutual_info=True),
      # TODO(b/128831096): Determine correct interaction between AMI and weights
      dict(
          testcase_name='adjusted_mi_binary_label_with_weights',
          feature_label_pairs=[
              (b'informative_1', 1),
              (b'informative_1', 1),
              (b'informative_0', 0),
              (b'informative_0', 0),
              (b'uninformative', 0),
              (b'uninformative', 1),
              (b'informative_by_weight', 0),
              (b'informative_by_weight', 1),
          ],
          # uninformative and informative_by_weight have the same co-occurrence
          # relationship with the label but will have different importance
          # values due to the weighting.
          expected_vocab=[
              (b'informative_0', 2.3029856),
              (b'informative_1', 0.3029896),
              (b'informative_by_weight', 0.1713041),
              (b'uninformative', -0.6969697),
          ],
          weights=[1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 5.0],
          use_adjusted_mutual_info=True),
      dict(
          testcase_name='adjusted_mi_min_diff_from_avg',
          feature_label_pairs=[
              (b'good_predictor_of_0', 0),
              (b'good_predictor_of_0', 0),
              (b'good_predictor_of_0', 0),
              (b'good_predictor_of_1', 1),
              (b'good_predictor_of_0', 0),
              (b'good_predictor_of_1', 1),
              (b'good_predictor_of_1', 1),
              (b'good_predictor_of_1', 1),
              (b'good_predictor_of_1', 0),
              (b'good_predictor_of_0', 1),
              (b'good_predictor_of_1', 1),
              (b'good_predictor_of_1', 1),
              (b'good_predictor_of_1', 1),
              (b'weak_predictor_of_1', 1),
              (b'weak_predictor_of_1', 0),
          ],
          # With min_diff_from_avg, the small AMI value is regularized to 0
          expected_vocab=[
              (b'good_predictor_of_0', 1.8322128),
              (b'good_predictor_of_1', 1.7554416),
              (b'weak_predictor_of_1', 0),
          ],
          use_adjusted_mutual_info=True,
          min_diff_from_avg=1),
  )
  def testVocabularyWithMutualInformation(self,
                                          feature_label_pairs,
                                          expected_vocab,
                                          weights=None,
                                          use_adjusted_mutual_info=False,
                                          min_diff_from_avg=0.0,
                                          feature_dtype=tf.string):
    input_data = []
    for (value, label) in feature_label_pairs:
      input_data.append({'x': value, 'label': label})
    feature_spec = {
        'x': tf.io.FixedLenFeature([], feature_dtype),
        'label': tf.io.FixedLenFeature([], tf.int64)
    }
    if weights is not None:
      feature_spec['weight'] = tf.io.FixedLenFeature([], tf.float32)
      assert len(weights) == len(input_data)
      for data, weight in zip(input_data, weights):
        data['weight'] = weight

    input_metadata = tft_unit.metadata_from_feature_spec(feature_spec)
    expected_metadata = input_metadata

    def preprocessing_fn(inputs):
      tft.vocabulary(
          inputs['x'],
          labels=inputs['label'],
          weights=inputs.get('weight'),
          store_frequency=True,
          vocab_filename='my_vocab',
          use_adjusted_mutual_info=use_adjusted_mutual_info,
          min_diff_from_avg=min_diff_from_avg)
      return inputs

    expected_data = input_data
    expected_vocab_file_contents = {
        'my_vocab': expected_vocab,
    }

    self.assertAnalyzeAndTransformResults(
        input_data,
        input_metadata,
        preprocessing_fn,
        expected_data,
        expected_metadata,
        expected_vocab_file_contents=expected_vocab_file_contents)

  def testVocabularyAnalyzerWithLabelsAndWeights(self):
    input_data = [
        {'a': 'hello', 'weights': .3, 'labels': 1},
        {'a': 'hello', 'weights': .4, 'labels': 1},
        {'a': 'hello', 'weights': .3, 'labels': 1},
        {'a': 'goodbye', 'weights': 1.2, 'labels': 1},
        {'a': 'aaaaa', 'weights': .6, 'labels': 1},
        {'a': 'aaaaa', 'weights': .7, 'labels': 1},
        {'a': 'goodbye', 'weights': 1., 'labels': 0},
        {'a': 'goodbye', 'weights': 1., 'labels': 0},
        {'a': 'aaaaa', 'weights': .6, 'labels': 1},
        {'a': 'aaaaa', 'weights': .7, 'labels': 1},
        {'a': 'goodbye', 'weights': 1., 'labels': 1},
        {'a': 'goodbye', 'weights': 1., 'labels': 0},
    ]
    input_metadata = tft_unit.metadata_from_feature_spec({
        'a': tf.io.FixedLenFeature([], tf.string),
        'weights': tf.io.FixedLenFeature([], tf.float32),
        'labels': tf.io.FixedLenFeature([], tf.int64)
    })
    expected_metadata = tft_unit.metadata_from_feature_spec({
        'index': tf.io.FixedLenFeature([], tf.int64),
    }, {
        'index': schema_pb2.IntDomain(min=-1, max=2, is_categorical=True),
    })

    def preprocessing_fn(inputs):
      return {
          'index':
              tft.compute_and_apply_vocabulary(
                  inputs['a'],
                  weights=inputs['weights'],
                  labels=inputs['labels'])
      }

    expected_data = [{
        'index': 2
    }, {
        'index': 2
    }, {
        'index': 2
    }, {
        'index': 1
    }, {
        'index': 0
    }, {
        'index': 0
    }, {
        'index': 1
    }, {
        'index': 1
    }, {
        'index': 0
    }, {
        'index': 0
    }, {
        'index': 1
    }, {
        'index': 1
    }]
    self.assertAnalyzeAndTransformResults(input_data, input_metadata,
                                          preprocessing_fn, expected_data,
                                          expected_metadata)

  def testVocabularyAnalyzerWithLabelsWeightsAndFrequency(self):
    input_data = [
        {'a': b'hello', 'weights': .3, 'labels': 1},
        {'a': b'hello', 'weights': .4, 'labels': 1},
        {'a': b'hello', 'weights': .3, 'labels': 1},
        {'a': b'goodbye', 'weights': 1.2, 'labels': 1},
        {'a': b'aaaaa', 'weights': .6, 'labels': 1},
        {'a': b'aaaaa', 'weights': .7, 'labels': 1},
        {'a': b'goodbye', 'weights': 1., 'labels': 0},
        {'a': b'goodbye', 'weights': 1., 'labels': 0},
        {'a': b'aaaaa', 'weights': .6, 'labels': 1},
        {'a': b'aaaaa', 'weights': .7, 'labels': 1},
        {'a': b'goodbye', 'weights': 1., 'labels': 1},
        {'a': b'goodbye', 'weights': 1., 'labels': 0},
    ]
    input_metadata = tft_unit.metadata_from_feature_spec({
        'a': tf.io.FixedLenFeature([], tf.string),
        'weights': tf.io.FixedLenFeature([], tf.float32),
        'labels': tf.io.FixedLenFeature([], tf.int64)
    })
    expected_metadata = input_metadata

    def preprocessing_fn(inputs):
      tft.vocabulary(
          inputs['a'],
          weights=inputs['weights'],
          labels=inputs['labels'],
          store_frequency=True,
          vocab_filename='my_vocab')
      return inputs

    expected_data = input_data
    expected_vocab_file_contents = {
        'my_vocab': [(b'aaaaa', 1.5637185), (b'goodbye', 0.8699492),
                     (b'hello', 0.6014302)]
    }

    self.assertAnalyzeAndTransformResults(
        input_data,
        input_metadata,
        preprocessing_fn,
        expected_data,
        expected_metadata,
        expected_vocab_file_contents=expected_vocab_file_contents)

  @tft_unit.named_parameters(
      dict(
          testcase_name='string_feature',
          features=['hello', 'world', 'goodbye', 'aaaaa', 'aaaaa', 'goodbye'],
          weights=[1.0, .5, 1.0, .26, .25, 1.5],
          feature_dtype=tf.string,
          expected_data=[1, 3, 0, 2, 2, 0],
      ),
      dict(
          testcase_name='int64_feature',
          features=[2, 1, 3, 4, 4, 3],
          weights=[1.0, .5, 1.0, .26, .25, 1.5],
          feature_dtype=tf.int64,
          expected_data=[1, 3, 0, 2, 2, 0],
      ))
  def testVocabularyAnalyzerWithWeights(self, features, weights, feature_dtype,
                                        expected_data):
    input_data = []
    for feature, weight in zip(features, weights):
      input_data.append({'a': feature, 'weights': weight})
    input_metadata = tft_unit.metadata_from_feature_spec({
        'a': tf.io.FixedLenFeature([], feature_dtype),
        'weights': tf.io.FixedLenFeature([], tf.float32)
    })
    expected_metadata = tft_unit.metadata_from_feature_spec({
        'index': tf.io.FixedLenFeature([], tf.int64),
    }, {'index': schema_pb2.IntDomain(min=-1, max=3, is_categorical=True)})

    def preprocessing_fn(inputs):
      return {
          'index':
              tft.compute_and_apply_vocabulary(
                  inputs['a'], weights=inputs['weights'])
      }

    expected_data = [{'index': val} for val in expected_data]

    self.assertAnalyzeAndTransformResults(input_data, input_metadata,
                                          preprocessing_fn, expected_data,
                                          expected_metadata)

  def testVocabularyAnalyzer(self):
    input_data = [
        {'a': 'hello'},
        {'a': 'world'},
        {'a': 'hello'},
        {'a': 'hello'},
        {'a': 'goodbye'},
        {'a': 'world'},
        {'a': 'aaaaa'},
        # Verify the analyzer can handle (dont-ignore) a space-only token.
        {'a': ' '},
        # Verify the analyzer can handle (ignore) the empty string.
        {'a': ''},
        # Verify the analyzer can handle (ignore) tokens that contain \n.
        {'a': '\n'},
        {'a': 'hi \n ho \n'},
        {'a': ' \r'},
    ]
    input_metadata = tft_unit.metadata_from_feature_spec(
        {'a': tf.io.FixedLenFeature([], tf.string)})
    expected_metadata = tft_unit.metadata_from_feature_spec({
        'index': tf.io.FixedLenFeature([], tf.int64),
    }, {
        'index': schema_pb2.IntDomain(min=-1, max=4, is_categorical=True),
    })

    # Assert empty string with default_value=-1
    def preprocessing_fn(inputs):
      return {'index': tft.compute_and_apply_vocabulary(inputs['a'])}

    expected_data = [
        {
            'index': 0
        },
        {
            'index': 1
        },
        {
            'index': 0
        },
        {
            'index': 0
        },
        {
            'index': 2
        },
        {
            'index': 1
        },
        {
            'index': 3
        },
        {
            'index': 4
        },
        # The empty string maps to compute_and_apply_vocabulary(
        #     default_value=-1).
        {
            'index': -1
        },
        # The tokens that contain \n map to
        # compute_and_apply_vocabulary(default_value=-1).
        {
            'index': -1
        },
        {
            'index': -1
        },
        {
            'index': -1
        }
    ]
    self.assertAnalyzeAndTransformResults(
        input_data, input_metadata, preprocessing_fn, expected_data,
        expected_metadata)

  def testVocabularyAnalyzerEmptyVocab(self):
    input_data = [
        {
            'a': 1
        },
        {
            'a': 2
        },
    ]
    input_metadata = tft_unit.metadata_from_feature_spec(
        {'a': tf.io.FixedLenFeature([], tf.int64)})

    # No tokens meet the frequency_threshold, so we should create an empty
    # vocabulary. All tokens will be treated as OOV.
    def preprocessing_fn(inputs):
      return {
          'index':
              tft.compute_and_apply_vocabulary(
                  inputs['a'], frequency_threshold=5)
      }

    expected_data = [
        {
            'index': -1
        },
        {
            'index': -1
        },
    ]
    self.assertAnalyzeAndTransformResults(input_data, input_metadata,
                                          preprocessing_fn, expected_data)

  def testVocabularyAnalyzerOOV(self):
    input_data = [
        {'a': 'hello'},
        {'a': 'world'},
        {'a': 'hello'},
        {'a': 'hello'},
        {'a': 'goodbye'},
        {'a': 'world'},
        {'a': 'aaaaa'},
        # Verify the analyzer can handle (dont-ignore) a space-only token.
        {'a': ' '},
        # Verify the analyzer can handle (ignore) the empty string.
        {'a': ''},
        # Verify the analyzer can handle (ignore) tokens that contain \n.
        {'a': '\n'},
        {'a': 'hi \n ho \n'},
        {'a': ' \r'},
    ]
    input_metadata = tft_unit.metadata_from_feature_spec(
        {'a': tf.io.FixedLenFeature([], tf.string)})

    # Assert empty string with num_oov_buckets=1
    def preprocessing_fn_oov(inputs):
      return {
          'index':
              tft.compute_and_apply_vocabulary(
                  inputs['a'], num_oov_buckets=1, vocab_filename='my_vocab')
      }
    expected_data = [
        {'index': 0},
        {'index': 1},
        {'index': 0},
        {'index': 0},
        {'index': 2},
        {'index': 1},
        {'index': 3},
        {'index': 4},
        # The empty string maps to the oov bucket.
        {'index': 5},
        # The tokens that contain \n map to the oov bucket.
        {'index': 5},
        {'index': 5},
        {'index': 5}
    ]
    expected_metadata = tft_unit.metadata_from_feature_spec({
        'index': tf.io.FixedLenFeature([], tf.int64),
    }, {
        'index': schema_pb2.IntDomain(min=0, max=5, is_categorical=True),
    })
    expected_vocab_file_contents = {
        'my_vocab': [b'hello', b'world', b'goodbye', b'aaaaa', b' ']
    }
    self.assertAnalyzeAndTransformResults(
        input_data, input_metadata, preprocessing_fn_oov, expected_data,
        expected_metadata,
        expected_vocab_file_contents=expected_vocab_file_contents)

  def testVocabularyAnalyzerPositiveNegativeIntegers(self):
    input_data = [
        {
            'a': 13
        },
        {
            'a': 14
        },
        {
            'a': 13
        },
        {
            'a': 13
        },
        {
            'a': 12
        },
        {
            'a': 14
        },
        {
            'a': 11
        },
        {
            'a': 10
        },
        {
            'a': 10
        },
        {
            'a': -10
        },
        {
            'a': -10
        },
        {
            'a': -20
        },
    ]
    input_metadata = tft_unit.metadata_from_feature_spec(
        {'a': tf.FixedLenFeature([], tf.int64)})

    def preprocessing_fn_oov(inputs):
      return {
          'b':
              tft.compute_and_apply_vocabulary(
                  inputs['a'], vocab_filename='my_vocab')
      }

    expected_data = [
        {
            'b': 0
        },
        {
            'b': 1
        },
        {
            'b': 0
        },
        {
            'b': 0
        },
        {
            'b': 4
        },
        {
            'b': 1
        },
        {
            'b': 5
        },
        {
            'b': 2
        },
        {
            'b': 2
        },
        {
            'b': 3
        },
        {
            'b': 3
        },
        {
            'b': 6
        },
    ]
    expected_metadata = tft_unit.metadata_from_feature_spec({
        'b': tf.FixedLenFeature([], tf.int64),
    }, {
        'b': schema_pb2.IntDomain(min=-1, max=6, is_categorical=True),
    })
    expected_vocab_file_contents = {
        'my_vocab': [b'13', b'14', b'10', b'-10', b'12', b'11', b'-20']
    }
    self.assertAnalyzeAndTransformResults(
        input_data,
        input_metadata,
        preprocessing_fn_oov,
        expected_data,
        expected_metadata=expected_metadata,
        expected_vocab_file_contents=expected_vocab_file_contents)

  def testCreateApplyVocab(self):
    input_data = [
        {'a': 'hello', 'b': 'world', 'c': 'aaaaa'},
        {'a': 'good', 'b': '', 'c': 'hello'},
        {'a': 'goodbye', 'b': 'hello', 'c': '\n'},
        {'a': ' ', 'b': 'aaaaa', 'c': 'bbbbb'}
    ]
    input_metadata = tft_unit.metadata_from_feature_spec({
        'a': tf.io.FixedLenFeature([], tf.string),
        'b': tf.io.FixedLenFeature([], tf.string),
        'c': tf.io.FixedLenFeature([], tf.string)
    })
    vocab_filename = 'test_compute_and_apply_vocabulary'
    expected_metadata = tft_unit.metadata_from_feature_spec({
        'index_a': tf.io.FixedLenFeature([], tf.int64),
        'index_b': tf.io.FixedLenFeature([], tf.int64),
    }, {
        'index_a': schema_pb2.IntDomain(min=-1, max=6, is_categorical=True),
        'index_b': schema_pb2.IntDomain(min=-1, max=6, is_categorical=True),
    })

    def preprocessing_fn(inputs):
      deferred_vocab_and_filename = tft.vocabulary(
          tf.concat([inputs['a'], inputs['b'], inputs['c']], 0),
          vocab_filename=vocab_filename)
      return {
          'index_a':
              tft.apply_vocabulary(inputs['a'], deferred_vocab_and_filename),
          'index_b':
              tft.apply_vocabulary(inputs['b'], deferred_vocab_and_filename)
      }

    expected_data = [
        # For tied frequencies, larger (lexicographic) items come first.
        # Index 5 corresponds to the word bbbbb.
        {'index_a': 0, 'index_b': 2},
        {'index_a': 4, 'index_b': -1},
        {'index_a': 3, 'index_b': 0},
        {'index_a': 6, 'index_b': 1}
    ]
    self.assertAnalyzeAndTransformResults(
        input_data, input_metadata, preprocessing_fn, expected_data,
        expected_metadata)

  # Example on how to use the vocab frequency as part of the transform
  # function.
  def testCreateVocabWithFrequency(self):
    input_data = [
        {'a': 'hello', 'b': 'world', 'c': 'aaaaa'},
        {'a': 'good', 'b': '', 'c': 'hello'},
        {'a': 'goodbye', 'b': 'hello', 'c': '\n'},
        {'a': '_', 'b': 'aaaaa', 'c': 'bbbbb'}
    ]
    input_metadata = tft_unit.metadata_from_feature_spec({
        'a': tf.io.FixedLenFeature([], tf.string),
        'b': tf.io.FixedLenFeature([], tf.string),
        'c': tf.io.FixedLenFeature([], tf.string)
    })
    vocab_filename = 'test_vocab_with_frequency'
    expected_metadata = tft_unit.metadata_from_feature_spec({
        'index_a': tf.io.FixedLenFeature([], tf.int64),
        'index_b': tf.io.FixedLenFeature([], tf.int64),
        'frequency_a': tf.io.FixedLenFeature([], tf.int64),
        'frequency_b': tf.io.FixedLenFeature([], tf.int64),
    }, {
        'index_a': schema_pb2.IntDomain(min=-1, max=6, is_categorical=True),
        'index_b': schema_pb2.IntDomain(min=-1, max=6, is_categorical=True),
        'frequency_a': schema_pb2.IntDomain(min=-1, max=6, is_categorical=True),
        'frequency_b': schema_pb2.IntDomain(min=-1, max=6, is_categorical=True),
    })

    def preprocessing_fn(inputs):
      deferred_vocab_and_filename = tft.vocabulary(
          tf.concat([inputs['a'], inputs['b'], inputs['c']], 0),
          vocab_filename=vocab_filename,
          store_frequency=True)

      def _apply_vocab(y, deferred_vocab_filename_tensor):
        # NOTE: Please be aware that TextFileInitializer assigns a special
        # meaning to the constant lookup_ops.TextFileIndex.LINE_NUMBER.
        table = lookup_ops.HashTable(
            lookup_ops.TextFileInitializer(
                deferred_vocab_filename_tensor,
                tf.string,
                1,
                tf.int64,
                lookup_ops.TextFileIndex.LINE_NUMBER,
                delimiter=' '),
            default_value=-1)
        table_size = table.size()
        return table.lookup(y), table_size

      def _apply_frequency(y, deferred_vocab_filename_tensor):
        table = lookup_ops.HashTable(
            lookup_ops.TextFileInitializer(
                deferred_vocab_filename_tensor,
                tf.string,
                1,
                tf.int64,
                0,
                delimiter=' '),
            default_value=-1)
        table_size = table.size()
        return table.lookup(y), table_size

      return {
          'index_a':
              tft.apply_vocabulary(
                  inputs['a'],
                  deferred_vocab_and_filename,
                  lookup_fn=_apply_vocab),
          'frequency_a':
              tft.apply_vocabulary(
                  inputs['a'],
                  deferred_vocab_and_filename,
                  lookup_fn=_apply_frequency),
          'index_b':
              tft.apply_vocabulary(
                  inputs['b'],
                  deferred_vocab_and_filename,
                  lookup_fn=_apply_vocab),
          'frequency_b':
              tft.apply_vocabulary(
                  inputs['b'],
                  deferred_vocab_and_filename,
                  lookup_fn=_apply_frequency),
      }

    expected_data = [
        # For tied frequencies, larger (lexicographic) items come first.
        # Index 5 corresponds to the word bbbbb.
        {'index_a': 0, 'frequency_a': 3, 'index_b': 2, 'frequency_b': 1},
        {'index_a': 4, 'frequency_a': 1, 'index_b': -1, 'frequency_b': -1},
        {'index_a': 3, 'frequency_a': 1, 'index_b': 0, 'frequency_b': 3},
        {'index_a': 6, 'frequency_a': 1, 'index_b': 1, 'frequency_b': 2}
    ]
    self.assertAnalyzeAndTransformResults(
        input_data, input_metadata, preprocessing_fn, expected_data,
        expected_metadata)

  def testVocabularyAnalyzerWithNDInputs(self):
    def preprocessing_fn(inputs):
      return {'index': tft.compute_and_apply_vocabulary(inputs['a'])}

    input_data = [
        {'a': [['some', 'say'], ['the', 'world']]},
        {'a': [['will', 'end'], ['in', 'fire']]},
        {'a': [['some', 'say'], ['in', 'ice']]},
    ]
    input_metadata = tft_unit.metadata_from_feature_spec(
        {'a': tf.io.FixedLenFeature([2, 2], tf.string)})
    expected_data = [
        {'index': [[0, 1], [5, 3]]},
        {'index': [[4, 8], [2, 7]]},
        {'index': [[0, 1], [2, 6]]},
    ]
    expected_metadata = tft_unit.metadata_from_feature_spec({
        'index': tf.io.FixedLenFeature([2, 2], tf.int64),
    }, {
        'index': schema_pb2.IntDomain(min=-1, max=8, is_categorical=True),
    })
    self.assertAnalyzeAndTransformResults(
        input_data, input_metadata, preprocessing_fn, expected_data,
        expected_metadata)

  def testVocabularyAnalyzerWithTokenization(self):
    def preprocessing_fn(inputs):
      return {
          'index':
              tft.compute_and_apply_vocabulary(tf.strings.split(inputs['a']))
      }

    input_data = [{'a': 'hello hello world'}, {'a': 'hello goodbye world'}]
    input_metadata = tft_unit.metadata_from_feature_spec(
        {'a': tf.io.FixedLenFeature([], tf.string)})
    expected_data = [{'index': [0, 0, 1]}, {'index': [0, 2, 1]}]

    expected_metadata = tft_unit.metadata_from_feature_spec({
        'index': tf.io.VarLenFeature(tf.int64),
    }, {
        'index': schema_pb2.IntDomain(min=-1, max=2, is_categorical=True),
    })
    self.assertAnalyzeAndTransformResults(
        input_data, input_metadata, preprocessing_fn, expected_data,
        expected_metadata)

  def testVocabularyAnalyzerWithTopK(self):
    def preprocessing_fn(inputs):
      return {
          'index1':
              tft.compute_and_apply_vocabulary(
                  tf.strings.split(inputs['a']), default_value=-99, top_k=2),

          # As above but using a string for top_k (and changing the
          # default_value to showcase things).
          'index2':
              tft.compute_and_apply_vocabulary(
                  tf.strings.split(inputs['a']), default_value=-9, top_k='2')
      }

    input_data = [
        {'a': 'hello hello world'},
        {'a': 'hello goodbye world'},
        {'a': 'hello goodbye foo'}
    ]
    input_metadata = tft_unit.metadata_from_feature_spec(
        {'a': tf.io.FixedLenFeature([], tf.string)})
    # Generated vocab (ordered by frequency, then value) should be:
    # ["hello", "world", "goodbye", "foo"]. After applying top_k=2, this becomes
    # ["hello", "world"].
    expected_data = [
        {'index1': [0, 0, 1], 'index2': [0, 0, 1]},
        {'index1': [0, -99, 1], 'index2': [0, -9, 1]},
        {'index1': [0, -99, -99], 'index2': [0, -9, -9]}
    ]

    expected_metadata = tft_unit.metadata_from_feature_spec({
        'index1': tf.io.VarLenFeature(tf.int64),
        'index2': tf.io.VarLenFeature(tf.int64),
    }, {
        'index1': schema_pb2.IntDomain(min=-99, max=1, is_categorical=True),
        'index2': schema_pb2.IntDomain(min=-9, max=1, is_categorical=True),
    })
    self.assertAnalyzeAndTransformResults(
        input_data, input_metadata, preprocessing_fn, expected_data,
        expected_metadata)

  def testVocabularyAnalyzerWithFrequencyThreshold(self):
    def preprocessing_fn(inputs):
      return {
          'index1':
              tft.compute_and_apply_vocabulary(
                  tf.strings.split(inputs['a']),
                  default_value=-99,
                  frequency_threshold=2),

          # As above but using a string for frequency_threshold (and changing
          # the default_value to showcase things).
          'index2':
              tft.compute_and_apply_vocabulary(
                  tf.strings.split(inputs['a']),
                  default_value=-9,
                  frequency_threshold='2')
      }

    input_data = [
        {'a': 'hello hello world'},
        {'a': 'hello goodbye world'},
        {'a': 'hello goodbye foo'}
    ]
    input_metadata = tft_unit.metadata_from_feature_spec(
        {'a': tf.io.FixedLenFeature([], tf.string)})
    # Generated vocab (ordered by frequency, then value) should be:
    # ["hello", "world", "goodbye", "foo"]. After applying frequency_threshold=2
    # this becomes
    # ["hello", "world", "goodbye"].
    expected_data = [
        {'index1': [0, 0, 1], 'index2': [0, 0, 1]},
        {'index1': [0, 2, 1], 'index2': [0, 2, 1]},
        {'index1': [0, 2, -99], 'index2': [0, 2, -9]}
    ]
    expected_metadata = tft_unit.metadata_from_feature_spec({
        'index1': tf.io.VarLenFeature(tf.int64),
        'index2': tf.io.VarLenFeature(tf.int64),
    }, {
        'index1': schema_pb2.IntDomain(min=-99, max=2, is_categorical=True),
        'index2': schema_pb2.IntDomain(min=-9, max=2, is_categorical=True),
    })
    self.assertAnalyzeAndTransformResults(
        input_data, input_metadata, preprocessing_fn, expected_data,
        expected_metadata)

  def testVocabularyAnalyzerWithFrequencyThresholdTooHigh(self):
    # Expected to return an empty dict due to too high threshold.
    def preprocessing_fn(inputs):
      return {
          'index1':
              tft.compute_and_apply_vocabulary(
                  tf.strings.split(inputs['a']),
                  default_value=-99,
                  frequency_threshold=77),

          # As above but using a string for frequency_threshold (and changing
          # the default_value to showcase things).
          'index2':
              tft.compute_and_apply_vocabulary(
                  tf.strings.split(inputs['a']),
                  default_value=-9,
                  frequency_threshold='77')
      }

    input_data = [
        {'a': 'hello hello world'},
        {'a': 'hello goodbye world'},
        {'a': 'hello goodbye foo'}
    ]
    input_metadata = tft_unit.metadata_from_feature_spec(
        {'a': tf.io.FixedLenFeature([], tf.string)})
    # Generated vocab (ordered by frequency, then value) should be:
    # ["hello", "world", "goodbye", "foo"]. After applying
    # frequency_threshold=77 this becomes empty.
    expected_data = [
        {'index1': [-99, -99, -99], 'index2': [-9, -9, -9]},
        {'index1': [-99, -99, -99], 'index2': [-9, -9, -9]},
        {'index1': [-99, -99, -99], 'index2': [-9, -9, -9]}
    ]
    # Note the vocabs are empty but the tables have size 1 so max_value is 1.
    expected_metadata = tft_unit.metadata_from_feature_spec({
        'index1': tf.io.VarLenFeature(tf.int64),
        'index2': tf.io.VarLenFeature(tf.int64),
    }, {
        'index1': schema_pb2.IntDomain(min=-99, max=0, is_categorical=True),
        'index2': schema_pb2.IntDomain(min=-9, max=0, is_categorical=True),
    })
    self.assertAnalyzeAndTransformResults(
        input_data, input_metadata, preprocessing_fn, expected_data,
        expected_metadata)

  def testVocabularyAnalyzerWithHighFrequencyThresholdAndOOVBuckets(self):
    def preprocessing_fn(inputs):
      return {
          'index1':
              tft.compute_and_apply_vocabulary(
                  tf.strings.split(inputs['a']),
                  default_value=-99,
                  top_k=1,
                  num_oov_buckets=3)
      }

    input_data = [
        {'a': 'hello hello world world'},
        {'a': 'hello tarkus toccata'},
        {'a': 'hello goodbye foo'}
    ]
    input_metadata = tft_unit.metadata_from_feature_spec(
        {'a': tf.io.FixedLenFeature([], tf.string)})
    # Generated vocab (ordered by frequency, then value) should be:
    # ["hello", "world", "goodbye", "foo", "tarkus", "toccata"]. After applying
    # top_k =1 this becomes ["hello"] plus three OOV buckets.
    # The specific output values here depend on the hash of the words, and the
    # test will break if the hash changes.
    expected_data = [
        {'index1': [0, 0, 2, 2]},
        {'index1': [0, 3, 1]},
        {'index1': [0, 2, 1]},
    ]
    expected_metadata = tft_unit.metadata_from_feature_spec({
        'index1': tf.io.VarLenFeature(tf.int64),
    }, {
        'index1': schema_pb2.IntDomain(min=0, max=3, is_categorical=True),
    })
    self.assertAnalyzeAndTransformResults(
        input_data, input_metadata, preprocessing_fn, expected_data,
        expected_metadata)

  def testPipelineWithoutAutomaterialization(self):
    # Other tests pass lists instead of PCollections and thus invoke
    # automaterialization where each call to a beam PTransform will implicitly
    # run its own pipeline.
    #
    # In order to test the case where PCollections are not materialized in
    # between calls to the tf.Transform PTransforms, we include a test that is
    # not based on automaterialization.
    def preprocessing_fn(inputs):
      return {'x_scaled': tft.scale_to_0_1(inputs['x'])}

    def equal_to(expected):

      def _equal(actual):
        dict_key_fn = lambda d: sorted(d.items())
        sorted_expected = sorted(expected, key=dict_key_fn)
        sorted_actual = sorted(actual, key=dict_key_fn)
        if sorted_expected != sorted_actual:
          raise ValueError('Failed assert: %s == %s' % (expected, actual))
      return _equal

    with self._makeTestPipeline() as pipeline:
      input_data = pipeline | 'CreateTrainingData' >> beam.Create(
          [{'x': 4}, {'x': 1}, {'x': 5}, {'x': 2}])
      metadata = tft_unit.metadata_from_feature_spec(
          {'x': tf.io.FixedLenFeature([], tf.float32)})
      with beam_impl.Context(temp_dir=self.get_temp_dir()):
        transform_fn = (
            (input_data, metadata)
            | 'AnalyzeDataset' >> beam_impl.AnalyzeDataset(preprocessing_fn))

      # Run transform_columns on some eval dataset.
      eval_data = pipeline | 'CreateEvalData' >> beam.Create(
          [{'x': 6}, {'x': 3}])
      transformed_eval_data, _ = (
          ((eval_data, metadata), transform_fn)
          | 'TransformDataset' >> beam_impl.TransformDataset())
      expected_data = [{'x_scaled': 1.25}, {'x_scaled': 0.5}]
      beam_test_util.assert_that(
          transformed_eval_data, equal_to(expected_data))

  def testNestedContextCreateBaseTempDir(self):
    level_1_dir = self.get_temp_dir()
    with beam_impl.Context(temp_dir=level_1_dir):
      self.assertEqual(
          os.path.join(level_1_dir, beam_impl.Context._TEMP_SUBDIR),
          beam_impl.Context.create_base_temp_dir())
      level_2_dir = self.get_temp_dir()
      with beam_impl.Context(temp_dir=level_2_dir):
        self.assertEqual(
            os.path.join(level_2_dir, beam_impl.Context._TEMP_SUBDIR),
            beam_impl.Context.create_base_temp_dir())
      self.assertEqual(
          os.path.join(level_1_dir, beam_impl.Context._TEMP_SUBDIR),
          beam_impl.Context.create_base_temp_dir())
    with self.assertRaises(ValueError):
      beam_impl.Context.create_base_temp_dir()

  @tft_unit.parameters(
      # Test for all integral types, each type is in a separate testcase to
      # increase parallelism of test shards (and reduce test time from ~250
      # seconds to ~80 seconds)
      *_construct_test_bucketization_parameters())
  def testBucketization(self, test_inputs, expected_boundaries, do_shuffle,
                        epsilon, should_apply, is_manual_boundaries,
                        input_dtype):
    test_inputs = list(test_inputs)

    # Shuffle the input to add randomness to input generated with
    # simple range().
    if do_shuffle:
      random.shuffle(test_inputs)

    def preprocessing_fn(inputs):
      x = tf.cast(inputs['x'], input_dtype)
      num_buckets = len(expected_boundaries) + 1
      if should_apply:
        if is_manual_boundaries:
          bucket_boundaries = expected_boundaries
        else:
          bucket_boundaries = tft.quantiles(inputs['x'], num_buckets, epsilon)
        result = tft.apply_buckets(x, bucket_boundaries)
      else:
        result = tft.bucketize(x, num_buckets=num_buckets, epsilon=epsilon)
      return {'q_b': result}

    input_data = [{'x': [x]} for x in test_inputs]

    input_metadata = tft_unit.metadata_from_feature_spec(
        {'x': tf.io.FixedLenFeature([1], _canonical_dtype(input_dtype))})

    # Sort the input based on value, index is used to create expected_data.
    indexed_input = enumerate(test_inputs)

    sorted_list = sorted(indexed_input, key=lambda p: p[1])

    # Expected data has the same size as input, one bucket per input value.
    expected_data = [None] * len(test_inputs)
    bucket = 0
    for (index, x) in sorted_list:
      # Increment the bucket number when crossing the boundary
      if (bucket < len(expected_boundaries) and
          x >= expected_boundaries[bucket]):
        bucket += 1
      expected_data[index] = {'q_b': [bucket]}

    expected_metadata = tft_unit.metadata_from_feature_spec({
        'q_b': tf.io.FixedLenFeature([1], tf.int64),
    }, {
        'q_b':
            schema_pb2.IntDomain(
                min=0, max=len(expected_boundaries), is_categorical=True),
    })

    @contextlib.contextmanager
    def no_assert():
      yield None

    assertion = no_assert()
    if input_dtype == tf.float16:
      assertion = self.assertRaisesRegexp(
          TypeError, '.*DataType float16 not in list of allowed values.*')

    with assertion:
      self.assertAnalyzeAndTransformResults(
          input_data,
          input_metadata,
          preprocessing_fn,
          expected_data,
          expected_metadata,
          desired_batch_size=1000,
          # TODO(b/110855155): Remove this explicit use of DirectRunner.
          beam_pipeline=beam.Pipeline())

  @tft_unit.parameters(
      # Test for all numerical types, each type is in a separate testcase to
      # increase parallelism of test shards and reduce test time.
      (tf.int32,),
      (tf.int64,),
      (tf.float32,),
      (tf.float64,),
      (tf.double,),
      # TODO(b/64836936): Enable test after bucket inconsistency is
      # fixed.
      # (tf.float16,)
  )
  def testQuantileBucketsWithWeights(self, input_dtype):

    def analyzer_fn(inputs):
      return {
          'q_b':
              tft.quantiles(
                  tf.cast(inputs['x'], input_dtype),
                  num_buckets=3,
                  epsilon=0.00001,
                  weights=inputs['weights'])
      }

    input_data = [{'x': [x], 'weights': [x / 100.]} for x in range(1, 3000)]
    input_metadata = tft_unit.metadata_from_feature_spec({
        'x': tf.io.FixedLenFeature([1], _canonical_dtype(input_dtype)),
        'weights': tf.io.FixedLenFeature([1], tf.float32)
    })
    # The expected data has 2 boundaries that divides the data into 3 buckets.
    expected_outputs = {'q_b': np.array([[1732, 2449]], np.float32)}
    self.assertAnalyzerOutputs(
        input_data,
        input_metadata,
        analyzer_fn,
        expected_outputs,
        desired_batch_size=1000)

  @tft_unit.parameters(
      # Test for all integral types, each type is in a separate testcase to
      # increase parallelism of test shards and reduce test time.
      (tf.int32,),
      (tf.int64,),
      (tf.float32,),
      (tf.float64,),
      (tf.double,),
      # TODO(b/64836936): Enable test after bucket inconsistency is
      # fixed.
      # (tf.float16,)
  )
  def testQuantileBuckets(self, input_dtype):

    def analyzer_fn(inputs):
      return {
          'q_b': tft.quantiles(tf.cast(inputs['x'], input_dtype),
                               num_buckets=3, epsilon=0.00001)
      }

    # NOTE: We force 3 batches: data has 3000 elements and we request a batch
    # size of 1000.
    input_data = [{'x': [x]} for  x in range(1, 3000)]
    input_metadata = tft_unit.metadata_from_feature_spec(
        {'x': tf.io.FixedLenFeature([1], _canonical_dtype(input_dtype))})
    # The expected data has 2 boundaries that divides the data into 3 buckets.
    expected_outputs = {'q_b': np.array([[1001, 2001]], np.float32)}
    self.assertAnalyzerOutputs(
        input_data,
        input_metadata,
        analyzer_fn,
        expected_outputs,
        desired_batch_size=1000)

  def testQuantilesPerKey(self):
    def analyzer_fn(inputs):
      key_vocab, q_b = analyzers._quantiles_per_key(
          inputs['x'], inputs['key'], num_buckets=3, epsilon=0.00001)
      return {
          'key_vocab': key_vocab,
          'q_b': q_b
      }

    # NOTE: We force 10 batches: data has 100 elements and we request a batch
    # size of 10.
    input_data = [{'x': [x], 'key': 'a' if x < 50 else 'b'}
                  for x in range(1, 100)]
    input_metadata = tft_unit.metadata_from_feature_spec({
        'x': tf.io.FixedLenFeature([1], tf.int64),
        'key': tf.io.FixedLenFeature([], tf.string)
    })
    # The expected data has 2 boundaries that divides the data into 3 buckets.
    expected_outputs = {
        'key_vocab': np.array([b'a', b'b'], np.object),
        'q_b': np.array([[17, 33], [66, 83]], np.float32)
    }
    self.assertAnalyzerOutputs(
        input_data,
        input_metadata,
        analyzer_fn,
        expected_outputs,
        desired_batch_size=10)

  def testBucketizePerKey(self):
    def preprocessing_fn(inputs):
      x_bucketized = tft.bucketize_per_key(
          inputs['x'], inputs['key'], num_buckets=3, epsilon=0.00001)
      return {
          'x_bucketized': x_bucketized
      }

    # NOTE: We force 10 batches: data has 100 elements and we request a batch
    # size of 10.
    input_data = [{'x': x, 'key': 'a' if x < 50 else 'b'}
                  for x in range(1, 100)]
    input_metadata = tft_unit.metadata_from_feature_spec({
        'x': tf.io.FixedLenFeature([], tf.float32),
        'key': tf.io.FixedLenFeature([], tf.string)
    })

    def compute_quantile(instance):
      if instance['key'] == 'a':
        if instance['x'] < 17:
          return 0
        elif instance['x'] < 33:
          return 1
        else:
          return 2
      else:
        if instance['x'] < 66:
          return 0
        elif instance['x'] < 83:
          return 1
        else:
          return 2

    expected_data = [{'x_bucketized': compute_quantile(instance)}
                     for instance in input_data]
    expected_metadata = tft_unit.metadata_from_feature_spec({
        'x_bucketized': tf.io.FixedLenFeature([], tf.int64),
    }, {
        'x_bucketized': schema_pb2.IntDomain(min=0, max=2, is_categorical=True),
    })
    self.assertAnalyzeAndTransformResults(
        input_data,
        input_metadata,
        preprocessing_fn,
        expected_data,
        expected_metadata,
        desired_batch_size=10)

  def testBucketizePerKeyWithInfrequentKeys(self):
    def preprocessing_fn(inputs):
      x_bucketized = tft.bucketize_per_key(
          inputs['x'], inputs['key'], num_buckets=4, epsilon=0.00001)
      return {
          'x_bucketized': x_bucketized
      }

    input_data = [
        {'x': [], 'key': []},
        {'x': [5, 6], 'key': ['a', 'a']},
        {'x': [7], 'key': ['a']},
        {'x': [12], 'key': ['b']},
        {'x': [13], 'key': ['b']},
        {'x': [15], 'key': ['c']},
        {'x': [2], 'key': ['d']},
        {'x': [4], 'key': ['d']},
        {'x': [6], 'key': ['d']},
        {'x': [8], 'key': ['d']},
        {'x': [2], 'key': ['e']},
        {'x': [4], 'key': ['e']},
        {'x': [6], 'key': ['e']},
        {'x': [8], 'key': ['e']},
        {'x': [10], 'key': ['e']},
        {'x': [11], 'key': ['e']},
        {'x': [12], 'key': ['e']},
        {'x': [13], 'key': ['e']}
    ]
    input_metadata = tft_unit.metadata_from_feature_spec({
        'x': tf.io.VarLenFeature(tf.float32),
        'key': tf.io.VarLenFeature(tf.string)
    })

    expected_data = [
        {'x_bucketized': []},
        {'x_bucketized': [1, 2]},
        {'x_bucketized': [3]},
        {'x_bucketized': [1]},
        {'x_bucketized': [3]},
        {'x_bucketized': [3]},
        {'x_bucketized': [0]},
        {'x_bucketized': [1]},
        {'x_bucketized': [2]},
        {'x_bucketized': [3]},
        {'x_bucketized': [0]},
        {'x_bucketized': [0]},
        {'x_bucketized': [1]},
        {'x_bucketized': [1]},
        {'x_bucketized': [2]},
        {'x_bucketized': [2]},
        {'x_bucketized': [3]},
        {'x_bucketized': [3]}
    ]
    expected_metadata = tft_unit.metadata_from_feature_spec({
        'x_bucketized': tf.io.VarLenFeature(tf.int64),
    }, {
        'x_bucketized': schema_pb2.IntDomain(min=0, max=3, is_categorical=True),
    })
    self.assertAnalyzeAndTransformResults(
        input_data,
        input_metadata,
        preprocessing_fn,
        expected_data,
        expected_metadata,
        desired_batch_size=10)

  def testBucketizePerKeySparse(self):
    def preprocessing_fn(inputs):
      x_bucketized = tft.bucketize_per_key(
          inputs['x'], inputs['key'], num_buckets=3, epsilon=0.00001)
      return {
          'x_bucketized': x_bucketized
      }

    # NOTE: We force 10 batches: data has 100 elements and we request a batch
    # size of 10.
    input_data = [{'x': [x], 'key': ['a'] if x < 50 else ['b']}
                  for x in range(1, 100)]
    input_metadata = tft_unit.metadata_from_feature_spec({
        'x': tf.io.VarLenFeature(tf.float32),
        'key': tf.io.VarLenFeature(tf.string)
    })

    def compute_quantile(instance):
      if instance['key'][0] == 'a':
        if instance['x'][0] < 17:
          return 0
        elif instance['x'][0] < 33:
          return 1
        else:
          return 2
      else:
        if instance['x'][0] < 66:
          return 0
        elif instance['x'][0] < 83:
          return 1
        else:
          return 2

    expected_data = [{'x_bucketized': [compute_quantile(instance)]}
                     for instance in input_data]
    expected_metadata = tft_unit.metadata_from_feature_spec({
        'x_bucketized': tf.io.VarLenFeature(tf.int64),
    }, {
        'x_bucketized': schema_pb2.IntDomain(min=0, max=2, is_categorical=True),
    })
    self.assertAnalyzeAndTransformResults(
        input_data,
        input_metadata,
        preprocessing_fn,
        expected_data,
        expected_metadata,
        desired_batch_size=10)

  def testVocabularyWithFrequency(self):
    outfile = 'vocabulary_with_frequency'
    def preprocessing_fn(inputs):

      # Force the analyzer to be executed, and store the frequency file as a
      # side-effect.
      _ = tft.vocabulary(
          inputs['a'], vocab_filename=outfile, store_frequency=True)
      _ = tft.vocabulary(inputs['a'], store_frequency=True)
      _ = tft.vocabulary(inputs['b'], store_frequency=True)

      # The following must not produce frequency output, just the vocab words.
      _ = tft.vocabulary(inputs['b'])
      a_int = tft.compute_and_apply_vocabulary(inputs['a'])

      # Return input unchanged, this preprocessing_fn is a no-op except for
      # computing uniques.
      return {'a_int': a_int}

    def check_asset_file_contents(assets_path, filename, expected):
      assets_file = os.path.join(assets_path, filename)
      with tf.io.gfile.GFile(assets_file, 'r') as f:
        contents = f.read()

      self.assertMultiLineEqual(expected, contents)

    input_metadata = tft_unit.metadata_from_feature_spec({
        'a': tf.io.FixedLenFeature([], tf.string),
        'b': tf.io.FixedLenFeature([], tf.string)
    })

    tft_tmp_dir = os.path.join(self.get_temp_dir(), 'temp_dir')
    transform_fn_dir = os.path.join(self.get_temp_dir(), 'export_transform_fn')

    with beam_impl.Context(temp_dir=tft_tmp_dir):
      with self._makeTestPipeline() as pipeline:
        input_data = pipeline | beam.Create([
            {'a': 'hello', 'b': 'hi'},
            {'a': 'world', 'b': 'ho ho'},
            {'a': 'hello', 'b': 'ho ho'},
        ])
        transform_fn = (
            (input_data, input_metadata)
            | beam_impl.AnalyzeDataset(preprocessing_fn))
        _ = transform_fn | transform_fn_io.WriteTransformFn(transform_fn_dir)

    self.assertTrue(os.path.isdir(tft_tmp_dir))

    saved_model_path = os.path.join(transform_fn_dir,
                                    tft.TFTransformOutput.TRANSFORM_FN_DIR)
    assets_path = os.path.join(saved_model_path,
                               tf.saved_model.ASSETS_DIRECTORY)
    self.assertTrue(os.path.isdir(assets_path))
    six.assertCountEqual(self, [
        outfile, 'vocab_frequency_vocabulary_1', 'vocab_frequency_vocabulary_2',
        'vocab_compute_and_apply_vocabulary_vocabulary', 'vocab_vocabulary_3'
    ], os.listdir(assets_path))

    check_asset_file_contents(assets_path, outfile,
                              '2 hello\n1 world\n')

    check_asset_file_contents(assets_path, 'vocab_frequency_vocabulary_1',
                              '2 hello\n1 world\n')

    check_asset_file_contents(assets_path, 'vocab_frequency_vocabulary_2',
                              '2 ho ho\n1 hi\n')

    check_asset_file_contents(assets_path, 'vocab_vocabulary_3',
                              'ho ho\nhi\n')

    check_asset_file_contents(assets_path,
                              'vocab_compute_and_apply_vocabulary_vocabulary',
                              'hello\nworld\n')

  @tft_unit.named_parameters(
      # fingerprints by which each of the tokens will be sorted if fingerprint
      # shuffling is used.
      # 'ho ho': '1b3dd735ddff70d90f3b7ba5ebf65df521d6ca4d'
      # 'world': '7c211433f02071597741e6ff5a8ea34789abbf43'
      # 'hello': 'aaf4c61ddcc5e8a2dabede0f3b482cd9aea9434d'
      # 'hi': 'c22b5f9178342609428d6f51b2c5af4c0bde6a42'
      dict(
          testcase_name='string_feature',
          a=['world', 'hello', 'hello'],
          b=['hi', 'ho ho', 'ho ho'],
          a_frequency_and_shuffle=[(b'world', 1), (b'hello', 2)],
          b_frequency_no_shuffle=[(b'ho ho', 2), (b'hi', 1)],
          a_shuffle_no_frequency=[b'world', b'hello'],
          a_no_shuffle_no_frequency=[b'hello', b'world'],
      ),
      # '1': '356a192b7913b04c54574d18c28d46e6395428ab'
      # '2': 'da4b9237bacccdf19c0760cab7aec4a8359010b0'
      # '3': '77de68daecd823babbb58edb1c8e14d7106e83bb'
      dict(
          testcase_name='int_feature',
          a=[1, 2, 2, 3],
          b=[2, 1, 1, 1],
          a_frequency_and_shuffle=[(b'1', 1), (b'3', 1), (b'2', 2)],
          b_frequency_no_shuffle=[(b'1', 3), (b'2', 1)],
          a_shuffle_no_frequency=[b'1', b'3', b'2'],
          a_no_shuffle_no_frequency=[b'2', b'3', b'1'],
          dtype=tf.int64,
      ))
  def testVocabularyWithFrequencyAndFingerprintShuffle(
      self,
      a,
      b,
      a_frequency_and_shuffle,
      b_frequency_no_shuffle,
      a_shuffle_no_frequency,
      a_no_shuffle_no_frequency,
      dtype=tf.string):

    def preprocessing_fn(inputs):
      # Sort by fingerprint.
      _ = tft.vocabulary(
          inputs['a'],
          vocab_filename='a_frequency_and_shuffle',
          store_frequency=True,
          fingerprint_shuffle=True)
      _ = tft.vocabulary(
          inputs['b'],
          store_frequency=True,
          vocab_filename='b_frequency_no_shuffle')

      # The following must not produce frequency output, just the shuffled vocab
      # words.
      _ = tft.vocabulary(
          inputs['a'],
          vocab_filename='a_shuffle_no_frequency',
          fingerprint_shuffle=True)
      # Sort by frequency (default).
      _ = tft.vocabulary(
          inputs['a'], vocab_filename='a_no_shuffle_no_frequency')
      # Simply return 'a'.
      return {'a': inputs['a']}

    input_metadata = tft_unit.metadata_from_feature_spec({
        'a': tf.io.FixedLenFeature([], dtype),
        'b': tf.io.FixedLenFeature([], dtype)
    })

    input_data = [{'a': a_val, 'b': b_val} for a_val, b_val in zip(a, b)]

    self.assertAnalyzeAndTransformResults(
        input_data,
        input_metadata,
        preprocessing_fn,
        expected_vocab_file_contents={
            'a_frequency_and_shuffle': a_frequency_and_shuffle,
            'b_frequency_no_shuffle': b_frequency_no_shuffle,
            'a_shuffle_no_frequency': a_shuffle_no_frequency,
            'a_no_shuffle_no_frequency': a_no_shuffle_no_frequency
        })

  def testCovarianceTwoDimensions(self):
    def analyzer_fn(inputs):
      return {'y': tft.covariance(inputs['x'], dtype=tf.float32)}

    input_data = [{'x': x} for x in [[0, 0], [4, 0], [2, -2], [2, 2]]]
    input_metadata = tft_unit.metadata_from_feature_spec(
        {'x': tf.io.FixedLenFeature([2], tf.float32)})
    expected_outputs = {'y': np.array([[2, 0], [0, 2]], np.float32)}
    self.assertAnalyzerOutputs(
        input_data, input_metadata, analyzer_fn, expected_outputs)

  def testCovarianceOneDimension(self):
    def analyzer_fn(inputs):
      return {'y': tft.covariance(inputs['x'], dtype=tf.float32)}

    input_data = [{'x': x} for x in [[0], [2], [4], [6]]]
    input_metadata = tft_unit.metadata_from_feature_spec(
        {'x': tf.io.FixedLenFeature([1], tf.float32)})
    expected_outputs = {'y': np.array([[5]], np.float32)}
    self.assertAnalyzerOutputs(
        input_data, input_metadata, analyzer_fn, expected_outputs)

  def testPCAThreeToTwoDimensions(self):
    def analyzer_fn(inputs):
      return {'y': tft.pca(inputs['x'], 2, dtype=tf.float32)}

    input_data = [{'x': x}
                  for x in  [[0, 0, 1], [4, 0, 1], [2, -1, 1], [2, 1, 1]]]
    input_metadata = tft_unit.metadata_from_feature_spec(
        {'x': tf.io.FixedLenFeature([3], tf.float32)})
    expected_outputs = {'y': np.array([[1, 0], [0, 1], [0, 0]], np.float32)}
    self.assertAnalyzerOutputs(
        input_data, input_metadata, analyzer_fn, expected_outputs)

  def _assert_quantile_boundaries(
      self, test_inputs, expected_boundaries, input_dtype, num_buckets=None,
      num_expected_buckets=None):

    if not num_buckets:
      num_buckets = len(expected_boundaries) + 1
    if not num_expected_buckets:
      num_expected_buckets = num_buckets

    def preprocessing_fn(inputs):
      x = tf.cast(inputs['x'], input_dtype)
      quantiles = tft.quantiles(x, num_buckets, epsilon=0.0001)
      quantiles.set_shape([1, num_expected_buckets - 1])
      return {
          'q_b': quantiles
      }

    input_data = [{'x': [x]} for x in test_inputs]

    input_metadata = tft_unit.metadata_from_feature_spec(
        {'x': tf.io.FixedLenFeature([1], _canonical_dtype(input_dtype))})

    # Expected data has the same size as input, one bucket per input value.
    batch_size = 1000
    expected_data = []
    num_batches = int(math.ceil(len(test_inputs) / float(batch_size)))

    for _ in range(num_batches):
      expected_data += [{'q_b': expected_boundaries}]

    expected_metadata = None

    self.assertAnalyzeAndTransformResults(
        input_data,
        input_metadata,
        preprocessing_fn,
        expected_data,
        expected_metadata,
        desired_batch_size=batch_size,
        # TODO(b/110855155): Remove this explicit use of DirectRunner.
        beam_pipeline=beam.Pipeline())

  def testBucketizationForTightSequence(self):
    # Divide a tight 1..N sequence into different number of buckets.
    self._assert_quantile_boundaries(
        [1, 2, 3, 4], [3], tf.int32, num_buckets=2)
    self._assert_quantile_boundaries(
        [1, 2, 3, 4], [3, 4], tf.int32, num_buckets=3)
    self._assert_quantile_boundaries(
        [1, 2, 3, 4], [2, 3, 4], tf.int32, num_buckets=4)
    self._assert_quantile_boundaries(
        [1, 2, 3, 4], [1, 2, 3, 4], tf.int32, num_buckets=5)
    # Request more number of buckets than there are inputs.
    self._assert_quantile_boundaries(
        [1, 2, 3, 4], [1, 2, 3, 4], tf.int32, num_buckets=6,
        num_expected_buckets=5)
    self._assert_quantile_boundaries(
        [1, 2, 3, 4], [1, 2, 3, 4], tf.int32, num_buckets=10,
        num_expected_buckets=5)

  def testBucketizationEqualDistributionInSequence(self):
    # Input pattern is of the form [1, 1, 1, ..., 2, 2, 2, ..., 3, 3, 3, ...]
    inputs = []
    for i in range(1, 101):
      inputs += [i] * 100
    # Expect 100 equally spaced buckets.
    expected_buckets = range(1, 101)
    self._assert_quantile_boundaries(
        inputs, expected_buckets, tf.int32, num_buckets=101)

  def testBucketizationEqualDistributionInterleaved(self):
    # Input pattern is of the form [1, 2, 3, ..., 1, 2, 3, ..., 1, 2, 3, ...]
    sequence = range(1, 101)
    inputs = []
    for _ in range(1, 101):
      inputs += sequence
    # Expect 100 equally spaced buckets.
    expected_buckets = range(1, 101)
    self._assert_quantile_boundaries(
        inputs, expected_buckets, tf.int32, num_buckets=101)

  def testBucketizationSpecificDistribution(self):
    # Distribution of input values.
    # This distribution is taken from one of the user pipelines.
    dist = (
        # Format: ((<min-value-in-range>, <max-value-in-range>), num-values)
        ((0.51, 0.67), 4013),
        ((0.67, 0.84), 2321),
        ((0.84, 1.01), 7145),
        ((1.01, 1.17), 64524),
        ((1.17, 1.34), 42886),
        ((1.34, 1.51), 154809),
        ((1.51, 1.67), 382678),
        ((1.67, 1.84), 582744),
        ((1.84, 2.01), 252221),
        ((2.01, 2.17), 7299))

    inputs = []
    for (mn, mx), num in dist:
      step = (mx - mn) / 100
      for ix in range(num//100):
        inputs += [mn + (ix * step)]

    expected_boundaries = [2.30900002, 3.56439996, 5.09719992, 7.07259989]

    self._assert_quantile_boundaries(
        inputs, expected_boundaries, tf.float32, num_buckets=5)

  class _SumCombiner(beam.PTransform):

    @staticmethod
    def _flatten_fn(batch_values):
      for value in zip(*batch_values):
        yield value

    @staticmethod
    def _sum_fn(values):
      return np.sum(list(values), axis=0)

    @staticmethod
    def _extract_outputs(sums):
      return [beam.pvalue.TaggedOutput('0', sums[0]),
              beam.pvalue.TaggedOutput('1', sums[1])]

    def expand(self, pcoll):
      output_tuple = (
          pcoll
          | beam.FlatMap(self._flatten_fn)
          | beam.CombineGlobally(self._sum_fn)
          | beam.FlatMap(self._extract_outputs).with_outputs('0', '1'))
      return (output_tuple['0'], output_tuple['1'])

  def testPTransformAnalyzer(self):

    def analyzer_fn(inputs):
      outputs = analyzers.ptransform_analyzer([inputs['x'], inputs['y']],
                                              [tf.int64, tf.int64],
                                              [[], []],
                                              self._SumCombiner())
      return {'x_sum': outputs[0], 'y_sum': outputs[1]}

    # NOTE: We force 10 batches: data has 100 elements and we request a batch
    # size of 10.
    input_data = [{'x': 1, 'y': i} for i in range(100)]
    input_metadata = tft_unit.metadata_from_feature_spec({
        'x': tf.io.FixedLenFeature([], tf.int64),
        'y': tf.io.FixedLenFeature([], tf.int64)
    })
    expected_outputs = {
        'x_sum': np.array(100, np.int64),
        'y_sum': np.array(4950, np.int64)
    }
    self.assertAnalyzerOutputs(
        input_data,
        input_metadata,
        analyzer_fn,
        expected_outputs,
        desired_batch_size=10)

  def testVocabularyAnalyzerWithKeyFn(self):
    def key_fn(string):
      return string.split(b'_X_')[0]

    def preprocessing_fn(inputs):
      return {
          'index1':
              tft.compute_and_apply_vocabulary(
                  tf.strings.split(inputs['a']),
                  coverage_top_k=1,
                  default_value=-99,
                  key_fn=key_fn,
                  frequency_threshold=3)
      }

    input_data = [
        {'a': 'a_X_1 a_X_1 a_X_2 b_X_1 b_X_2'},
        {'a': 'a_X_1 a_X_1 a_X_2 a_X_2'},
        {'a': 'b_X_2'}
    ]
    input_metadata = tft_unit.metadata_from_feature_spec(
        {'a': tf.io.FixedLenFeature([], tf.string)})

    expected_data = [
        {'index1': [0, 0, 1, -99, 2]},
        {'index1': [0, 0, 1, 1]},
        {'index1': [2]}
    ]
    expected_metadata = tft_unit.metadata_from_feature_spec({
        'index1': tf.io.VarLenFeature(tf.int64),
    }, {
        'index1': schema_pb2.IntDomain(min=-99, max=2, is_categorical=True),
    })
    self.assertAnalyzeAndTransformResults(
        input_data, input_metadata, preprocessing_fn, expected_data,
        expected_metadata)

  def testVocabularyAnalyzerWithKeyFnAndMultiCoverageTopK(self):
    def key_fn(string):
      return string.split(b'_X_')[0]

    def preprocessing_fn(inputs):
      return {
          'index1':
              tft.compute_and_apply_vocabulary(
                  tf.strings.split(inputs['a']),
                  coverage_top_k=2,
                  default_value=-99,
                  key_fn=key_fn,
                  frequency_threshold=300)
      }

    input_data = [
        {'a': 'a_X_1 a_X_1 a_X_2 b_X_1 b_X_2'},
        {'a': 'a_X_1 a_X_1 a_X_2 a_X_2 a_X_3'},
        {'a': 'b_X_2'}
    ]
    input_metadata = tft_unit.metadata_from_feature_spec(
        {'a': tf.io.FixedLenFeature([], tf.string)})

    expected_data = [
        {'index1': [0, 0, 1, 3, 2]},
        {'index1': [0, 0, 1, 1, -99]},
        {'index1': [2]}
    ]
    expected_metadata = tft_unit.metadata_from_feature_spec({
        'index1': tf.io.VarLenFeature(tf.int64),
    }, {
        'index1': schema_pb2.IntDomain(min=-99, max=3, is_categorical=True),
    })
    self.assertAnalyzeAndTransformResults(
        input_data, input_metadata, preprocessing_fn, expected_data,
        expected_metadata)

  def testVocabularyAnalyzerWithKeyFnAndTopK(self):
    def key_fn(string):
      return string.split(b'_X_')[0]

    def preprocessing_fn(inputs):
      return {
          'index1':
              tft.compute_and_apply_vocabulary(
                  tf.strings.split(inputs['a']),
                  coverage_top_k=1,
                  default_value=-99,
                  key_fn=key_fn,
                  top_k=2)
      }

    input_data = [
        {'a': 'a_X_1 a_X_1 a_X_2 b_X_1 b_X_2'},
        {'a': 'a_X_1 a_X_1 a_X_2 a_X_2'},
        {'a': 'b_X_2 b_X_2 b_X_2 b_X_2 c_X_1'}
    ]
    input_metadata = tft_unit.metadata_from_feature_spec(
        {'a': tf.io.FixedLenFeature([], tf.string)})

    expected_data = [
        {'index1': [1, 1, -99, -99, 0]},
        {'index1': [1, 1, -99, -99]},
        {'index1': [0, 0, 0, 0, 2]}
    ]
    expected_metadata = tft_unit.metadata_from_feature_spec({
        'index1': tf.io.VarLenFeature(tf.int64),
    }, {
        'index1': schema_pb2.IntDomain(min=-99, max=2, is_categorical=True),
    })
    self.assertAnalyzeAndTransformResults(
        input_data, input_metadata, preprocessing_fn, expected_data,
        expected_metadata)

  def testVocabularyAnalyzerWithKeyFnMultiCoverageTopK(self):
    def key_fn(string):
      return string.split(b'_X_')[1]

    def preprocessing_fn(inputs):
      return {
          'index1':
              tft.compute_and_apply_vocabulary(
                  tf.strings.split(inputs['a']),
                  coverage_top_k=2,
                  default_value=-99,
                  key_fn=key_fn,
                  frequency_threshold=4)
      }

    input_data = [
        {'a': '0_X_a 0_X_a 5_X_a 6_X_a 6_X_a 0_X_a'},
        {'a': '0_X_a 2_X_a 2_X_a 2_X_a 0_X_a 5_X_a'},
        {'a': '1_X_b 1_X_b 3_X_b 3_X_b 0_X_b 1_X_b 1_X_b'}
    ]
    input_metadata = tft_unit.metadata_from_feature_spec(
        {'a': tf.io.FixedLenFeature([], tf.string)})

    expected_data = [
        {'index1': [0, 0, -99, -99, -99, 0]},
        {'index1': [0, 2, 2, 2, 0, -99]},
        {'index1': [1, 1, 3, 3, -99, 1, 1]}
    ]
    expected_metadata = tft_unit.metadata_from_feature_spec({
        'index1': tf.io.VarLenFeature(tf.int64),
    }, {
        'index1': schema_pb2.IntDomain(min=-99, max=3, is_categorical=True),
    })
    self.assertAnalyzeAndTransformResults(
        input_data, input_metadata, preprocessing_fn, expected_data,
        expected_metadata)

  def testVocabularyWithKeyFnAndFrequency(self):
    def key_fn(string):
      return string.split(b'_X_')[1]

    outfile = 'vocabulary_with_frequency'

    def preprocessing_fn(inputs):

      # Force the analyzer to be executed, and store the frequency file as a
      # side-effect.

      _ = tft.vocabulary(
          tf.strings.split(inputs['a']),
          coverage_top_k=1,
          key_fn=key_fn,
          frequency_threshold=4,
          vocab_filename=outfile,
          store_frequency=True)

      _ = tft.vocabulary(
          tf.strings.split(inputs['a']),
          coverage_top_k=1,
          key_fn=key_fn,
          frequency_threshold=4,
          store_frequency=True)

      a_int = tft.compute_and_apply_vocabulary(
          tf.strings.split(inputs['a']),
          coverage_top_k=1,
          key_fn=key_fn,
          frequency_threshold=4)

      # Return input unchanged, this preprocessing_fn is a no-op except for
      # computing uniques.
      return {'a_int': a_int}

    def check_asset_file_contents(assets_path, filename, expected):
      assets_file = os.path.join(assets_path, filename)
      with tf.io.gfile.GFile(assets_file, 'r') as f:
        contents = f.read()

      self.assertMultiLineEqual(expected, contents)

    input_metadata = tft_unit.metadata_from_feature_spec(
        {'a': tf.io.FixedLenFeature([], tf.string)})

    tft_tmp_dir = os.path.join(self.get_temp_dir(), 'temp_dir')
    transform_fn_dir = os.path.join(self.get_temp_dir(), 'export_transform_fn')

    with beam_impl.Context(temp_dir=tft_tmp_dir):
      with self._makeTestPipeline() as pipeline:
        input_data = pipeline | beam.Create([
            {'a': '1_X_a 1_X_a 2_X_a 1_X_b 2_X_b'},
            {'a': '1_X_a 1_X_a 2_X_a 2_X_a'},
            {'a': '2_X_b 3_X_c 4_X_c'}
        ])

        transform_fn = (
            (input_data, input_metadata)
            | beam_impl.AnalyzeDataset(preprocessing_fn))
        _ = transform_fn | transform_fn_io.WriteTransformFn(transform_fn_dir)

    self.assertTrue(os.path.isdir(tft_tmp_dir))

    saved_model_path = os.path.join(transform_fn_dir,
                                    tft.TFTransformOutput.TRANSFORM_FN_DIR)
    assets_path = os.path.join(saved_model_path,
                               tf.saved_model.ASSETS_DIRECTORY)
    self.assertTrue(os.path.isdir(assets_path))

    check_asset_file_contents(assets_path, outfile,
                              '4 1_X_a\n2 2_X_b\n1 4_X_c\n')

  def testVocabularyAnalyzerWithKeyFnAndWeights(self):
    def key_fn(string):
      return string[0]

    input_data = [
        {'a': 'xa', 'weights': 1.},
        {'a': 'xa', 'weights': .5},
        {'a': 'xb', 'weights': 3},
        {'a': 'ya', 'weights': .6},
        {'a': 'yb', 'weights': .25},
        {'a': 'yc', 'weights': .5},
    ]
    input_metadata = tft_unit.metadata_from_feature_spec({
        'a': tf.io.FixedLenFeature([], tf.string),
        'weights': tf.io.FixedLenFeature([], tf.float32)
    })
    expected_metadata = tft_unit.metadata_from_feature_spec({
        'index': tf.io.FixedLenFeature([], tf.int64),
    }, {
        'index': schema_pb2.IntDomain(min=-1, max=1, is_categorical=True),
    })

    def preprocessing_fn(inputs):
      return {
          'index':
              tft.compute_and_apply_vocabulary(
                  inputs['a'], weights=inputs['weights'], coverage_top_k=1,
                  key_fn=key_fn, frequency_threshold=1.5,
                  coverage_frequency_threshold=1)
      }

    expected_data = [
        {'index': 1},
        {'index': 1},
        {'index': 0},
        {'index': -1},
        {'index': -1},
        {'index': -1},
    ]
    self.assertAnalyzeAndTransformResults(input_data, input_metadata,
                                          preprocessing_fn, expected_data,
                                          expected_metadata)

  def testVocabularyAnalyzerWithKeyFnAndLabels(self):
    def key_fn(string):
      return string[:2]

    input_data = [
        {'a': 'aaa', 'labels': 1},
        {'a': 'aaa', 'labels': 1},
        {'a': 'aaa', 'labels': 1},
        {'a': 'aab', 'labels': 1},
        {'a': 'aba', 'labels': 0},
        {'a': 'aba', 'labels': 1},
        {'a': 'aab', 'labels': 0},
        {'a': 'aab', 'labels': 0},
        {'a': 'aba', 'labels': 0},
        {'a': 'abc', 'labels': 1},
        {'a': 'abc', 'labels': 1},
        {'a': 'aab', 'labels': 0}
    ]
    input_metadata = tft_unit.metadata_from_feature_spec({
        'a': tf.io.FixedLenFeature([], tf.string),
        'labels': tf.io.FixedLenFeature([], tf.int64)
    })
    expected_metadata = tft_unit.metadata_from_feature_spec({
        'index': tf.io.FixedLenFeature([], tf.int64),
    }, {
        'index': schema_pb2.IntDomain(min=-1, max=1, is_categorical=True),
    })

    def preprocessing_fn(inputs):
      return {
          'index':
              tft.compute_and_apply_vocabulary(
                  inputs['a'], key_fn=key_fn, labels=inputs['labels'],
                  coverage_top_k=1, frequency_threshold=3)
      }

    expected_data = [
        {'index': 0},
        {'index': 0},
        {'index': 0},
        {'index': -1},
        {'index': -1},
        {'index': -1},
        {'index': -1},
        {'index': -1},
        {'index': -1},
        {'index': 1},
        {'index': 1},
        {'index': -1}]
    self.assertAnalyzeAndTransformResults(input_data, input_metadata,
                                          preprocessing_fn, expected_data,
                                          expected_metadata)

  def testSavedModelWithAnnotations(self):
    """Test serialization/deserialization as a saved model with annotations."""
    # TODO(b/132098015): Schema annotations aren't yet supported in OSS builds.
    # pylint: disable=g-import-not-at-top
    try:
      from tensorflow_transform import annotations_pb2
    except ImportError:
      return
    # pylint: enable=g-import-not-at-top
    def preprocessing_fn(inputs):
      # Bucketization applies annotations to the output schema
      return {
          'x_bucketized': tft.bucketize(inputs['x'], num_buckets=4),
      }

    input_data = [{'x': 1}, {'x': 2}, {'x': 3}, {'x': 4}]
    input_metadata = tft_unit.metadata_from_feature_spec({
        'x': tf.io.FixedLenFeature([], tf.float32),
    })
    temp_dir = self.get_temp_dir()
    with beam_impl.Context(temp_dir=temp_dir):
      transform_fn = ((input_data, input_metadata)
                      | beam_impl.AnalyzeDataset(preprocessing_fn))
      #  Write transform_fn to serialize annotation collections to SavedModel
      _ = transform_fn | transform_fn_io.WriteTransformFn(temp_dir)

    # Ensure that the annotations survive the round trip to SavedModel.
    tf_transform_output = tft.TFTransformOutput(temp_dir)
    savedmodel_dir = tf_transform_output.transform_savedmodel_dir
    schema = beam_impl._infer_metadata_from_saved_model(savedmodel_dir)._schema
    self.assertLen(schema._schema_proto.feature, 1)
    for feature in schema._schema_proto.feature:
      self.assertLen(feature.annotation.extra_metadata, 1)
      for annotation in feature.annotation.extra_metadata:
        message = annotations_pb2.BucketBoundaries()
        annotation.Unpack(message)
        self.assertAllClose(list(message.boundaries), [2, 3, 4])

  def testSavedModelWithGlobalAnnotations(self):
    # TODO(b/132098015): Schema annotations aren't yet supported in OSS builds.
    # pylint: disable=g-import-not-at-top
    try:
      from tensorflow_transform import annotations_pb2
    except ImportError:
      return
    # pylint: enable=g-import-not-at-top
    def preprocessing_fn(inputs):
      # Add some arbitrary annotation data at the global schema level.
      boundaries = tf.constant([[1.0]])
      message_type = annotations_pb2.BucketBoundaries.DESCRIPTOR.full_name
      sizes = tf.expand_dims([tf.size(boundaries)], axis=0)
      message_proto = encode_proto_op.encode_proto(
          sizes, [tf.cast(boundaries, tf.float32)], ['boundaries'],
          message_type)[0]
      type_url = os.path.join('type.googleapis.com', message_type)
      schema_inference.annotate(type_url, message_proto)
      return {
          'x_scaled': tft.scale_by_min_max(inputs['x']),
      }

    input_data = [{'x': 1}, {'x': 2}, {'x': 3}, {'x': 4}]
    input_metadata = tft_unit.metadata_from_feature_spec({
        'x': tf.io.FixedLenFeature([], tf.float32),
    })
    temp_dir = self.get_temp_dir()
    with beam_impl.Context(temp_dir=temp_dir):
      transform_fn = ((input_data, input_metadata)
                      | beam_impl.AnalyzeDataset(preprocessing_fn))
      #  Write transform_fn to serialize annotation collections to SavedModel
      _ = transform_fn | transform_fn_io.WriteTransformFn(temp_dir)

    # Ensure that global annotations survive the round trip to SavedModel.
    tf_transform_output = tft.TFTransformOutput(temp_dir)
    savedmodel_dir = tf_transform_output.transform_savedmodel_dir
    schema = beam_impl._infer_metadata_from_saved_model(savedmodel_dir)._schema
    self.assertLen(schema._schema_proto.annotation.extra_metadata, 1)
    for annotation in schema._schema_proto.annotation.extra_metadata:
      message = annotations_pb2.BucketBoundaries()
      annotation.Unpack(message)
      self.assertAllClose(list(message.boundaries), [1])


if __name__ == '__main__':
  tft_unit.main()
