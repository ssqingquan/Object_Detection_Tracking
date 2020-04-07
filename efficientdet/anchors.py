# Copyright 2020 Google Research. All Rights Reserved.
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
"""Anchor definition.

This module is borrowed from TPU RetinaNet implementation:
https://github.com/tensorflow/tpu/blob/master/models/official/retinanet/anchors.py
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import numpy as np
import tensorflow.compat.v1 as tf
from .object_detection import argmax_matcher
from .object_detection import box_list
from .object_detection import faster_rcnn_box_coder
from .object_detection import region_similarity_calculator
from .object_detection import target_assigner

# The minimum score to consider a logit for identifying detections.
MIN_CLASS_SCORE = -5.0

# The score for a dummy detection
_DUMMY_DETECTION_SCORE = -1e5

# The maximum number of (anchor,class) pairs to keep for non-max suppression.
#MAX_DETECTION_POINTS = 5000

# The maximum number of detections per image.
MAX_DETECTIONS_PER_IMAGE = 100


def sigmoid(x):
  """Sigmoid function for use with Numpy for CPU evaluation."""
  return 1 / (1 + np.exp(-x))


def decode_box_outputs(rel_codes, anchors):
  """Transforms relative regression coordinates to absolute positions.

  Network predictions are normalized and relative to a given anchor; this
  reverses the transformation and outputs absolute coordinates for the input
  image.

  Args:
    rel_codes: box regression targets.
    anchors: anchors on all feature levels.
  Returns:
    outputs: bounding boxes.

  """
  ycenter_a = (anchors[0] + anchors[2]) / 2
  xcenter_a = (anchors[1] + anchors[3]) / 2
  ha = anchors[2] - anchors[0]
  wa = anchors[3] - anchors[1]
  ty, tx, th, tw = rel_codes

  w = np.exp(tw) * wa
  h = np.exp(th) * ha
  ycenter = ty * ha + ycenter_a
  xcenter = tx * wa + xcenter_a
  ymin = ycenter - h / 2.
  xmin = xcenter - w / 2.
  ymax = ycenter + h / 2.
  xmax = xcenter + w / 2.
  return np.column_stack([ymin, xmin, ymax, xmax])


def nms(dets, thresh):
  """Non-maximum suppression."""
  x1 = dets[:, 0]
  y1 = dets[:, 1]
  x2 = dets[:, 2]
  y2 = dets[:, 3]
  scores = dets[:, 4]

  areas = (x2 - x1 + 1) * (y2 - y1 + 1)
  order = scores.argsort()[::-1]

  keep = []
  while order.size > 0:
    i = order[0]
    keep.append(i)
    xx1 = np.maximum(x1[i], x1[order[1:]])
    yy1 = np.maximum(y1[i], y1[order[1:]])
    xx2 = np.minimum(x2[i], x2[order[1:]])
    yy2 = np.minimum(y2[i], y2[order[1:]])

    w = np.maximum(0.0, xx2 - xx1 + 1)
    h = np.maximum(0.0, yy2 - yy1 + 1)
    intersection = w * h
    overlap = intersection / (areas[i] + areas[order[1:]] - intersection)

    inds = np.where(overlap <= thresh)[0]
    order = order[inds + 1]
  return keep


def _generate_anchor_configs(min_level, max_level, num_scales, aspect_ratios):
  """Generates mapping from output level to a list of anchor configurations.

  A configuration is a tuple of (num_anchors, scale, aspect_ratio).

  Args:
      min_level: integer number of minimum level of the output feature pyramid.
      max_level: integer number of maximum level of the output feature pyramid.
      num_scales: integer number representing intermediate scales added
        on each level. For instances, num_scales=2 adds two additional
        anchor scales [2^0, 2^0.5] on each level.
      aspect_ratios: list of tuples representing the aspect ratio anchors added
        on each level. For instances, aspect_ratios =
        [(1, 1), (1.4, 0.7), (0.7, 1.4)] adds three anchors on each level.
  Returns:
    anchor_configs: a dictionary with keys as the levels of anchors and
      values as a list of anchor configuration.
  """
  anchor_configs = {}
  for level in range(min_level, max_level + 1):
    anchor_configs[level] = []
    for scale_octave in range(num_scales):
      for aspect in aspect_ratios:
        anchor_configs[level].append(
            (2**level, scale_octave / float(num_scales), aspect))
  return anchor_configs


def _generate_anchor_boxes(image_size, anchor_scale, anchor_configs,
                           image_height):
  """Generates multiscale anchor boxes.

  Args:
    image_size: integer number of input image size. The input image has the
      same dimension for width and height. The image_size should be divided by
      the largest feature stride 2^max_level.
    anchor_scale: float number representing the scale of size of the base
      anchor to the feature stride 2^level.
    anchor_configs: a dictionary with keys as the levels of anchors and
      values as a list of anchor configuration.
  Returns:
    anchor_boxes: a numpy array with shape [N, 4], which stacks anchors on all
      feature levels.
  Raises:
    ValueError: input size must be the multiple of largest feature stride.
  """
  boxes_all = []
  for _, configs in anchor_configs.items():
    boxes_level = []
    for config in configs:
      stride, octave_scale, aspect = config
      if image_size % stride != 0:
        raise ValueError("input size must be divided by the stride.")
      base_anchor_size = anchor_scale * stride * 2**octave_scale
      anchor_size_x_2 = base_anchor_size * aspect[0] / 2.0
      anchor_size_y_2 = base_anchor_size * aspect[1] / 2.0

      x = np.arange(stride / 2, image_size, stride)
      #y = np.arange(stride / 2, image_size, stride)
      y = np.arange(stride / 2, image_height, stride)
      xv, yv = np.meshgrid(x, y)
      xv = xv.reshape(-1)
      yv = yv.reshape(-1)

      boxes = np.vstack((yv - anchor_size_y_2, xv - anchor_size_x_2,
                         yv + anchor_size_y_2, xv + anchor_size_x_2))
      boxes = np.swapaxes(boxes, 0, 1)
      boxes_level.append(np.expand_dims(boxes, axis=1))
    # concat anchors on the same level to the reshape NxAx4
    boxes_level = np.concatenate(boxes_level, axis=1)
    boxes_all.append(boxes_level.reshape([-1, 4]))

  anchor_boxes = np.vstack(boxes_all)
  return anchor_boxes


def _generate_detections(cls_outputs, box_outputs, anchor_boxes, indices,
                         classes, image_id, image_scale, num_classes,
                         level_index):
  """Generates detections with RetinaNet model outputs and anchors.

  Args:
    cls_outputs: a numpy array with shape [N, 1], which has the highest class
      scores on all feature levels. The N is the number of selected
      top-K total anchors on all levels.  (k being MAX_DETECTION_POINTS)
    box_outputs: a numpy array with shape [N, 4], which stacks box regression
      outputs on all feature levels. The N is the number of selected top-k
      total anchors on all levels. (k being MAX_DETECTION_POINTS)
    anchor_boxes: a numpy array with shape [N, 4], which stacks anchors on all
      feature levels. The N is the number of selected top-k total anchors on
      all levels.
    indices: a numpy array with shape [N], which is the indices from top-k
      selection.
    classes: a numpy array with shape [N], which represents the class
      prediction on all selected anchors from top-k selection.
    image_id: an integer number to specify the image id.
    image_scale: a float tensor representing the scale between original image
      and input image for the detector. It is used to rescale detections for
      evaluating with the original groundtruth annotations.
    num_classes: a integer that indicates the number of classes.
    # added by Junwei
    level_index: [N], which feature level each box is from.
  Returns:
    detections: detection results in a tensor with each row representing
      [image_id, x, y, width, height, score, class]
  """
  anchor_boxes = anchor_boxes[indices, :]

  # [5000,]
  scores = sigmoid(cls_outputs)
  # apply bounding box regression to anchors
  boxes = decode_box_outputs(
      box_outputs.swapaxes(0, 1), anchor_boxes.swapaxes(0, 1))
  # [5000, 4]
  boxes = boxes[:, [1, 0, 3, 2]]
  # run class-wise nms
  detections = []
  for c in range(num_classes):  # [0 - 89]
  # a num_class int vector, 1 means keep this class
  #for c, keep in enumerate(target_classes):
  #  if keep == 0:
  #    continue
    # classes: [5000,], each is 1-90
    # [K], K indices from [1-5000] that classes == c
    indices = np.where(classes == c)[0]
    if indices.shape[0] == 0:
      continue
    boxes_cls = boxes[indices, :]
    scores_cls = scores[indices]
    level_index_cls = level_index[indices]
    # Select top-scoring boxes in each class and apply non-maximum suppression
    # (nms) for boxes in the same class. The selected boxes from each class are
    # then concatenated for the final detection outputs.
    # [K, 5]
    all_detections_cls = np.column_stack((boxes_cls, scores_cls))
    # [R]
    top_detection_idx = nms(all_detections_cls, 0.5)
    # [R, 5]
    top_detections_cls = all_detections_cls[top_detection_idx]
    # we want [x1, y1, x2, y2]
    #top_detections_cls[:, 2] -= top_detections_cls[:, 0]
    #top_detections_cls[:, 3] -= top_detections_cls[:, 1]
    # [R]
    top_detections_level_index = level_index_cls[top_detection_idx]
    # [R, 7] # [image_id, box4, score, classindex1-90]
    # now [R, 8] # with level index at the end
    top_detections_cls = np.column_stack(
        (np.repeat(image_id, len(top_detection_idx)),
         top_detections_cls,
         np.repeat(c + 1, len(top_detection_idx)),
         top_detections_level_index)
    )

    detections.append(top_detections_cls)

  """
  def _generate_dummy_detections(number):
    detections_dummy = np.zeros((number, 8), dtype=np.float32)
    detections_dummy[:, 0] = image_id[0]
    detections_dummy[:, 5] = _DUMMY_DETECTION_SCORE
    detections_dummy[:, 7] = 7  # dummy feature level
    return detections_dummy
  """
  if detections:
    detections = np.vstack(detections)
    # take final 100 detections
    # change from -2 to -3 by Junwei
    indices = np.argsort(-detections[:, -3])
    detections = np.array(
        detections[indices[0:MAX_DETECTIONS_PER_IMAGE]], dtype=np.float32)
    # Add dummy detections to fill up to 100 detections
    #n = max(MAX_DETECTIONS_PER_IMAGE - len(detections), 0)
    #detections_dummy = _generate_dummy_detections(n)
    #detections = np.vstack([detections, detections_dummy])
    #[R, 8]
    detections[:, 1:5] *= image_scale
    # separete them so we can add feature pooling on the boxes
    # boxes, score, class, level_index
    # TODO: replace them with tf ops
    return (detections[:, 1:5], detections[:, 5],
            detections[:, 6], detections[:, 7])
  else:
    return ([], [], [], [])



class Anchors(object):
  """RetinaNet Anchors class."""

  def __init__(self, min_level, max_level, num_scales, aspect_ratios,
               anchor_scale, image_size, image_height):
    """Constructs multiscale RetinaNet anchors.

    Args:
      min_level: integer number of minimum level of the output feature pyramid.
      max_level: integer number of maximum level of the output feature pyramid.
      num_scales: integer number representing intermediate scales added
        on each level. For instances, num_scales=2 adds two additional
        anchor scales [2^0, 2^0.5] on each level.
      aspect_ratios: list of tuples representing the aspect ratio anchors added
        on each level. For instances, aspect_ratios =
        [(1, 1), (1.4, 0.7), (0.7, 1.4)] adds three anchors on each level.
      anchor_scale: float number representing the scale of size of the base
        anchor to the feature stride 2^level.
      image_size: integer number of input image size. The input image has the
        same dimension for width and height. The image_size should be divided by
        the largest feature stride 2^max_level.
    """
    self.min_level = min_level
    self.max_level = max_level
    self.num_scales = num_scales
    self.aspect_ratios = aspect_ratios
    self.anchor_scale = anchor_scale
    self.image_size = image_size
    self.image_height = image_height
    self.config = self._generate_configs()
    self.boxes = self._generate_boxes()

  def _generate_configs(self):
    """Generate configurations of anchor boxes."""
    return _generate_anchor_configs(self.min_level, self.max_level,
                                    self.num_scales, self.aspect_ratios)

  def _generate_boxes(self):
    """Generates multiscale anchor boxes."""
    boxes = _generate_anchor_boxes(self.image_size, self.anchor_scale,
                                   self.config, self.image_height)
    boxes = tf.convert_to_tensor(boxes, dtype=tf.float32)
    return boxes

  def get_anchors_per_location(self):
    return self.num_scales * len(self.aspect_ratios)


class AnchorLabeler(object):
  """Labeler for multiscale anchor boxes."""

  def __init__(self, anchors, num_classes, match_threshold=0.5):
               #partial_classes=[]):
    """Constructs anchor labeler to assign labels to anchors.

    Args:
      anchors: an instance of class Anchors.
      num_classes: integer number representing number of classes in the dataset.
      match_threshold: float number between 0 and 1 representing the threshold
        to assign positive labels for anchors.
    """
    similarity_calc = region_similarity_calculator.IouSimilarity()
    matcher = argmax_matcher.ArgMaxMatcher(
        match_threshold,
        unmatched_threshold=match_threshold,
        negatives_lower_than_unmatched=True,
        force_match_for_each_row=True)
    box_coder = faster_rcnn_box_coder.FasterRcnnBoxCoder()

    self._target_assigner = target_assigner.TargetAssigner(
        similarity_calc, matcher, box_coder)
    self._anchors = anchors
    self._match_threshold = match_threshold

    self._num_classes = num_classes
    """
    self._target_classes = np.ones((num_classes), dtype="int")
    if partial_classes:
      self._target_classes = np.zeros((num_classes), dtype="int")
      for class_idx in partial_classes:
        self._target_classes[class_idx] = 1
    """

  def _unpack_labels(self, labels):
    """Unpacks an array of labels into multiscales labels."""
    labels_unpacked = collections.OrderedDict()
    anchors = self._anchors
    count = 0
    for level in range(anchors.min_level, anchors.max_level + 1):
      feat_size = int(anchors.image_size / 2**level)
      feat_size_height = int(anchors.image_height / 2**level)
      #steps = feat_size**2 * anchors.get_anchors_per_location()
      steps = feat_size_height*feat_size * anchors.get_anchors_per_location()
      indices = tf.range(count, count + steps)
      count += steps
      labels_unpacked[level] = tf.reshape(
          tf.gather(labels, indices), [feat_size_height, feat_size, -1])
    return labels_unpacked

  def label_anchors(self, gt_boxes, gt_labels):
    """Labels anchors with ground truth inputs.

    Args:
      gt_boxes: A float tensor with shape [N, 4] representing groundtruth boxes.
        For each row, it stores [y0, x0, y1, x1] for four corners of a box.
      gt_labels: A integer tensor with shape [N, 1] representing groundtruth
        classes.
    Returns:
      cls_targets_dict: ordered dictionary with keys
        [min_level, min_level+1, ..., max_level]. The values are tensor with
        shape [height_l, width_l, num_anchors]. The height_l and width_l
        represent the dimension of class logits at l-th level.
      box_targets_dict: ordered dictionary with keys
        [min_level, min_level+1, ..., max_level]. The values are tensor with
        shape [height_l, width_l, num_anchors * 4]. The height_l and
        width_l represent the dimension of bounding box regression output at
        l-th level.
      num_positives: scalar tensor storing number of positives in an image.
    """
    gt_box_list = box_list.BoxList(gt_boxes)
    anchor_box_list = box_list.BoxList(self._anchors.boxes)

    # cls_weights, box_weights are not used
    cls_targets, _, box_targets, _, matches = self._target_assigner.assign(
        anchor_box_list, gt_box_list, gt_labels)

    # class labels start from 1 and the background class = -1
    cls_targets -= 1
    cls_targets = tf.cast(cls_targets, tf.int32)

    # Unpack labels.
    cls_targets_dict = self._unpack_labels(cls_targets)
    box_targets_dict = self._unpack_labels(box_targets)
    num_positives = tf.reduce_sum(
        tf.cast(tf.not_equal(matches.match_results, -1), tf.float32))

    return cls_targets_dict, box_targets_dict, num_positives

  def generate_detections(self, cls_outputs, box_outputs, indices, classes,
                          image_id, image_scale, level_index):
    return tf.py_func(_generate_detections, [
        cls_outputs, box_outputs, self._anchors.boxes, indices, classes,
        image_id, image_scale, self._num_classes, level_index,
        #image_id, image_scale, self._target_classes, level_index,
    ], [tf.float32, tf.float32, tf.float32, tf.float32])
