from csbdeep.utils import normalize
import cv2
import importlib
import itertools
import math
import numpy as np
import os
from scipy.stats import binned_statistic
from sklearn.linear_model import LinearRegression
import threading
from typing import Union

from project import app
from project.lib.datamanagement.models import EggLayingImage
from project.lib.image.chamber import CT
from project.lib.image.converter import byte_to_bgr
from project.lib.util import distance, trueRegions, COL_G

dirname = os.path.dirname(__file__)
ARENA_IMG_RESIZE_FACTOR = 0.186
UNET_SETTINGS = {
    "config_path": "project/configs/unet_reduced_backbone_arena_wells.json",
    "n_channel": 3,
    "weights_path": os.path.join(dirname, "../../models/arena_pit_v2.pth"),
}
torch_found = importlib.util.find_spec("torch") is not None
if torch_found:
    import torch
    from project.detectors.splinedist.config import Config
    from project.detectors.splinedist.models.model2d import SplineDist2D
if torch_found and torch.cuda.is_available():
    unet_config = Config(UNET_SETTINGS["config_path"], UNET_SETTINGS["n_channel"])
    default_model = SplineDist2D(unet_config)
    default_model.cuda()
    default_model.train(False)
    default_model.load_state_dict(torch.load(UNET_SETTINGS["weights_path"]))
else:
    default_model = None


def centroidnp(arr):
    """Return the column-wise averages for a first 2 columns of a Numpy array.

    Arguments:
      - arr: Numpy array with at least two columns.
    """
    length = arr.shape[0]
    sum_x = np.sum(arr[:, 0])
    sum_y = np.sum(arr[:, 1])
    return sum_x / length, sum_y / length


def fit_circle_kasa(points):
    """
    Perform a simple algebraic circle fit (Kåsa's method).

    points: (N,2) array of (x, y) coordinates along the circle's outline
    Returns (cx, cy, r)
    """
    x = points[:, 0]
    y = points[:, 1]
    # Build the linear system: M * [A, B, C]^T = Y
    # where the circle eqn is x^2 + y^2 + A x + B y + C = 0
    M = np.column_stack((x, y, np.ones_like(x)))
    w = x**2 + y**2
    Y = -w

    # Solve for A, B, C in least squares sense
    A, B, C = np.linalg.lstsq(M, Y, rcond=None)[0]

    # Convert back to center-radius form
    cx = -A / 2
    cy = -B / 2
    r = np.sqrt(cx**2 + cy**2 - C)

    return cx, cy, r


# corner-finding (source: https://stackoverflow.com/a/20354078/13312013)
def fake_image_corners(xy_sequence):
    """Get an approximation of image corners based on available data."""
    all_x, all_y = [
        tuple(xy_sequence[:, :, i].flatten()) for i in range(xy_sequence.shape[-1])
    ]
    min_x, max_x, min_y, max_y = min(all_x), max(all_x), min(all_y), max(all_y)
    d = dict()
    d["tl"] = min_x, min_y
    d["tr"] = max_x, min_y
    d["bl"] = min_x, max_y
    d["br"] = max_x, max_y
    return d


def fake_image_corners_old(xy_sequence):
    """Get an approximation of image corners based on available data."""
    all_x, all_y = zip(*xy_sequence)
    min_x, max_x, min_y, max_y = min(all_x), max(all_x), min(all_y), max(all_y)
    d = dict()
    d["tl"] = min_x, min_y
    d["tr"] = max_x, min_y
    d["bl"] = min_x, max_y
    d["br"] = max_x, max_y
    return d


def corners(xy_sequence, image_corners):
    """Return a dict with the best point for each corner."""
    d = dict()
    seq_shape = xy_sequence.shape
    xy_sequence = xy_sequence.reshape(seq_shape[0] * seq_shape[1], -1)
    d["tl"] = min(xy_sequence, key=lambda xy: distance(xy, image_corners["tl"]))
    d["tr"] = min(xy_sequence, key=lambda xy: distance(xy, image_corners["tr"]))
    d["bl"] = min(xy_sequence, key=lambda xy: distance(xy, image_corners["bl"]))
    d["br"] = min(xy_sequence, key=lambda xy: distance(xy, image_corners["br"]))
    return d


def corners_old(xy_sequence, image_corners):
    """Return a dict with the best point for each corner."""
    d = dict()
    d["tl"] = min(xy_sequence, key=lambda xy: distance(xy, image_corners["tl"]))
    d["tr"] = min(xy_sequence, key=lambda xy: distance(xy, image_corners["tr"]))
    d["bl"] = min(xy_sequence, key=lambda xy: distance(xy, image_corners["bl"]))
    d["br"] = min(xy_sequence, key=lambda xy: distance(xy, image_corners["br"]))
    return d


# end corner-finding


def getChamberTypeByRowsAndCols(numRowsCols):
    """Return a chamber type name based on a match with the number of rows and
    columns.

    Arguments:
      - numRowsCols: list of the form [numRows, numCols]

    Output:
      - ct: Chamber class for the given type
      - inverted: boolean indicating whether rows and columns were flipped
                  to find the chamber type.
    """
    for ct in CT:
        if (
            numRowsCols[0] == ct.value().numRows
            and numRowsCols[1] == ct.value().numCols
        ) or (
            numRowsCols[0] == ct.value().numCols
            and numRowsCols[1] == ct.value().numRows
        ):
            return ct.name, numRowsCols[0] == ct.value().numCols
    return None, False


def subImagesFromGridPoints(img, xs, ys):
    """Split an image into a grid determined by the inputted X and Y coordinates.
    The returned images are organized in a row-dominant order.

    Arguments:
      - img: an image to segment
      - xs: grid points along X axis
      - ys: grid points along Y axis
    """
    subImgs = []
    for i, y in enumerate(ys):
        for j, x in enumerate(xs):
            subImgs.append(img[y : ys[j + 1], x : xs[i + 1]])
    return subImgs


def subImagesFromBBoxes(img, bboxes):
    """Split an image according to the inputted bounding boxes (whose order
    determines the order of the returned sub-images).

    Arguments:
      - img: an image to segment
      - bboxes: a list of bounding boxes. Each bounding box is a list of the form
                [x_min, y_min, width, height] in pixels.
    """
    subImgs = []
    for bbox in bboxes:
        subImgs.append(img[bbox[1] : bbox[1] + bbox[3], bbox[0] : bbox[0] + bbox[2]])
    return subImgs


class CircleFinder:
    """Detect landmarks in the center of the egg-laying chambers to use in
    segmenting the image.
    """

    def __init__(
        self,
        img_name: str,
        img_shape: Union[tuple, list],
        room: str,
        allowSkew: bool = False,
        model=default_model,
        predict_resize_factor: float = ARENA_IMG_RESIZE_FACTOR,
        img=None,
    ):
        """Create new CircleFinder instance.

        Arguments:
          - img_name: the basename of the image
          - img_shape: tuple of (image_height, image_width)
          - room: ID of the user session (used for download image from DB if needed)
          - allowSkew: boolean which, if set to its default value of False, registers
                       an error if skew is detected in the image.
          - model: landmark-detection model to use. Currently, only SplineDist-based
                   models are supported, or at least models with a predict_instances
                   method whose return values mirror those from SplineDist.
                   Defaults to the currently best-performing model.
          - predict_resize_factor: factor by which the image is scaled before being
                                   inputted to the model.
        """
        self.img_name = img_name
        self.img_shape = img_shape
        self.img = img
        self.room = room
        self.skewed = None
        self.allowSkew = allowSkew
        self.model = model
        self.predict_resize_factor = predict_resize_factor

    def getPixelToMMRatio(self):
        """Calculate the image's ratio of pixels to mm, averaged between the result
        for rows and for columns."""
        self.pxToMM = 0.5 * (
            self.avgDists[0 if self.inverted else 1] / CT[self.ct].value().rowDist
            + self.avgDists[1 if self.inverted else 0] / CT[self.ct].value().colDist
        )

    def findAgaroseWells(self, img, centers, pxToMM, cannyParam1=40, cannyParam2=35):
        circles = cv2.HoughCircles(
            img,
            cv2.HOUGH_GRADIENT,
            1,
            140,
            param1=cannyParam1,
            param2=cannyParam2,
            minRadius=30,
            maxRadius=50,
        )
        self.shortest_distances = {}
        self.grouped_circles = {}
        self.well_to_well_slopes = {}
        circles = np.uint16(np.around(circles))
        dist_threshold = 0.5 * 0.25 * CT.large.value().floorSideLength * pxToMM

        for i, center in enumerate(centers):
            center = np.round(np.multiply(center, 0.25)).astype(np.int)
            for circ in circles[0, :]:
                circ = circ.astype(np.int32)
                to_well_dist = distance(circ[:2], center)
                if to_well_dist > dist_threshold:
                    continue
                else:
                    if i in self.grouped_circles:
                        self.grouped_circles[i]["raw"].append(circ.tolist())
                    else:
                        self.grouped_circles[i] = {"raw": [circ.tolist()]}
                if (
                    i not in self.shortest_distances
                    or to_well_dist < self.shortest_distances[i]
                ):
                    self.shortest_distances[i] = to_well_dist
            if i not in self.grouped_circles or len(self.grouped_circles[i]["raw"]) < 2:
                continue
            (
                leftmost,
                rightmost,
                uppermost,
                lowermost,
            ) = self.getRelativePositionsOfAgaroseWells(i, center)
            self.well_to_well_slopes[i] = []
            if None not in (lowermost, uppermost):
                self.well_to_well_slopes[i].append(
                    (uppermost[0] - lowermost[0]) / (uppermost[1] - lowermost[1])
                )
            if None not in (leftmost, rightmost):
                self.well_to_well_slopes[i].append(
                    -(rightmost[1] - leftmost[1]) / (rightmost[0] - leftmost[0])
                )
        self.skew_slopes = [
            el for sub_l in list(self.well_to_well_slopes.values()) for el in sub_l
        ]

    def getLargeChamberBBoxesAndImages(self, centers, pxToMM):
        if getattr(self, "img", None) is not None:
            img = self.img
        else:
            with app.app_context():
                img = byte_to_bgr(
                    EggLayingImage.query.filter_by(
                        session_id=self.room, basename=self.img_name
                    )
                    .first()
                    .image
                )
        bboxes = []
        img = cv2.medianBlur(img, 5)
        img_for_circles = cv2.resize(
            cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), (0, 0), fx=0.25, fy=0.25
        ).astype(np.uint8)

        self.findAgaroseWells(img_for_circles, centers, pxToMM)
        if len(self.skew_slopes) == 0 or set(range(4)) != set(
            self.grouped_circles.keys()
        ):
            self.findAgaroseWells(
                img_for_circles, centers, pxToMM, cannyParam1=35, cannyParam2=30
            )

        center_to_agarose_dist = np.mean(list(self.shortest_distances.values())) / 0.25
        if len(self.skew_slopes) > 0:
            skew_slope = np.mean(self.skew_slopes)
            rotation_angle = math.atan(skew_slope)
        else:
            rotation_angle = 0
        skew_slope = np.mean(self.skew_slopes)
        rotation_angle = math.atan(skew_slope)
        for i, center in enumerate(centers):
            acrossCircleD = round(10.5 * pxToMM)
            halfRealCircleD = round((0.5 * 10) * pxToMM)
            deltas = {
                "up": {
                    "x": -halfRealCircleD,
                    "y": -(center_to_agarose_dist + halfRealCircleD),
                },
                "right": {
                    "x": center_to_agarose_dist - halfRealCircleD,
                    "y": -halfRealCircleD,
                },
                "down": {
                    "x": -halfRealCircleD,
                    "y": center_to_agarose_dist - halfRealCircleD,
                },
                "left": {
                    "x": -(center_to_agarose_dist + halfRealCircleD),
                    "y": -halfRealCircleD,
                },
            }
            for position in deltas:
                if i in self.grouped_circles and position in self.grouped_circles[i]:
                    bboxes = CircleFinder.addUpperLeftCornerToBBox(
                        bboxes,
                        np.divide(self.grouped_circles[i][position][:2], 0.25),
                        -halfRealCircleD,
                        -halfRealCircleD,
                        0,
                    )
                    bboxes = CircleFinder.addWidthAndHeightToBBox(
                        bboxes, acrossCircleD, 0
                    )
                else:
                    self.interpolate_well_position(
                        bboxes,
                        center,
                        deltas,
                        acrossCircleD,
                        halfRealCircleD,
                        rotation_angle,
                        position,
                        i,
                    )
        return bboxes

    def interpolate_well_position(
        self,
        bboxes,
        center,
        deltas,
        acrossCircleD,
        halfRealCircleD,
        rotation_angle,
        position,
        i,
    ):
        if len(self.grouped_circles[i]) < 4:
            bboxes = CircleFinder.addUpperLeftCornerToBBox(
                bboxes,
                center,
                deltas[position]["x"],
                deltas[position]["y"],
                rotation_angle,
            )
        else:
            for k in ("left", "up", "down", "right"):
                if k not in self.grouped_circles[i]:
                    missing_well = k
                    break
            rotation_angle = math.atan(self.well_to_well_slopes[i][0])
            if missing_well in ("right", "left"):
                well_pair_keys = ("up", "down")
            else:
                well_pair_keys = ("right", "left")
            well_pair_dist = (
                4
                * 0.5
                * distance(
                    self.grouped_circles[i][well_pair_keys[0]],
                    self.grouped_circles[i][well_pair_keys[1]],
                )
            )
            sign = 1 if missing_well in ("right", "down") else -1
            well_pair_midpoint = [
                4 * 0.5 * el
                for el in [
                    (
                        self.grouped_circles[i][well_pair_keys[0]][0]
                        + self.grouped_circles[i][well_pair_keys[1]][0]
                    ),
                    (
                        self.grouped_circles[i][well_pair_keys[0]][1]
                        + self.grouped_circles[i][well_pair_keys[1]][1]
                    ),
                ]
            ]
            distance_coefficients = [
                sign * math.cos(rotation_angle),
                sign
                * math.sin(
                    rotation_angle * (-1 if missing_well in ("left", "right") else 1)
                ),
            ]
            if missing_well in ("up", "down"):
                distance_coefficients = list(reversed(distance_coefficients))
            well_center = [
                well_pair_midpoint[0] + well_pair_dist * distance_coefficients[0],
                well_pair_midpoint[1] + well_pair_dist * distance_coefficients[1],
            ]
            bboxes = CircleFinder.addUpperLeftCornerToBBox(
                bboxes,
                well_center,
                -halfRealCircleD,
                -halfRealCircleD,
                0,
            )
        bboxes = CircleFinder.addWidthAndHeightToBBox(
            bboxes, acrossCircleD, rotation_angle
        )

    def getRelativePositionsOfAgaroseWells(self, i, center):
        leftmost, rightmost, uppermost, lowermost = None, None, None, None
        for circ in self.grouped_circles[i]["raw"]:
            if type(leftmost) == type(None) or circ[0] < leftmost[0]:
                leftmost = circ
        for circ in self.grouped_circles[i]["raw"]:
            if circ == leftmost:
                continue
            if circ[0] - leftmost[0] < 100 or abs(center[0] - leftmost[0]) < 100:
                leftmost = None
                break
        if leftmost is not None:
            self.grouped_circles[i]["left"] = leftmost
            self.grouped_circles[i]["raw"].remove(leftmost)
        for circ in self.grouped_circles[i]["raw"]:
            if type(rightmost) == type(None) or circ[0] > rightmost[0]:
                rightmost = circ
        for circ in self.grouped_circles[i]["raw"] + [leftmost]:
            if None in (rightmost, circ) or circ == rightmost:
                continue
            if rightmost[0] - circ[0] < 100:
                rightmost = None
                break
            if leftmost is not None and abs(leftmost[1] - rightmost[1]) > 20:
                rightmost = None
                break
        if rightmost is not None:
            self.grouped_circles[i]["right"] = rightmost
            self.grouped_circles[i]["raw"].remove(rightmost)
        for circ in self.grouped_circles[i]["raw"]:
            if type(uppermost) == type(None) or circ[1] < uppermost[1]:
                uppermost = circ
        for circ in self.grouped_circles[i]["raw"] + [leftmost, rightmost]:
            if None in (uppermost, circ) or circ == uppermost:
                continue
            if circ[1] - uppermost[1] < 100:
                uppermost = None
        if uppermost is not None:
            self.grouped_circles[i]["up"] = uppermost
            self.grouped_circles[i]["raw"].remove(uppermost)
        for circ in self.grouped_circles[i]["raw"]:
            if type(lowermost) == type(None) or circ[1] > lowermost[1]:
                lowermost = circ
        for circ in self.grouped_circles[i]["raw"] + [leftmost, rightmost, uppermost]:
            if None in (lowermost, circ) or circ == lowermost:
                continue
            if lowermost[1] - circ[1] < 100:
                lowermost = None
        if lowermost is not None:
            self.grouped_circles[i]["down"] = lowermost
            self.grouped_circles[i]["raw"].remove(lowermost)
        return leftmost, rightmost, uppermost, lowermost

    @staticmethod
    def addUpperLeftCornerToBBox(bboxes, center, x_del, y_del, rotation_angle):
        bboxes.append(
            [
                round(
                    max(
                        center[0]
                        + x_del * math.cos(rotation_angle)
                        + y_del * math.sin(rotation_angle),
                        0,
                    )
                ),
                round(
                    max(
                        center[1]
                        - x_del * math.sin(rotation_angle)
                        + y_del * math.cos(rotation_angle),
                        0,
                    )
                ),
            ]
        )
        return bboxes

    @staticmethod
    def addWidthAndHeightToBBox(bboxes, delta, rotation_angle):
        bboxes[-1] += [
            round(
                bboxes[-1][0]
                + delta * (-math.sin(rotation_angle) + math.cos(rotation_angle))
            )
            - round(bboxes[-1][0]),
            round(
                bboxes[-1][1]
                + delta * (math.cos(rotation_angle) + math.sin(rotation_angle))
            )
            - round(bboxes[-1][1]),
        ]
        return bboxes

    @staticmethod
    def getSubImagesFromBBoxes(img, bboxes, ignore_indices=None):
        sub_imgs = []
        for i, bbox in enumerate(bboxes):
            if ignore_indices and ignore_indices[i]:
                sub_imgs.append(None)
                continue
            sub_imgs.append(
                img[bbox[1] : bbox[1] + bbox[3], bbox[0] : bbox[0] + bbox[2]]
            )
        return sub_imgs

    def getSubImageBBoxes(self, centers, avgDists, numRowsCols):
        """Determine sub-images for the image based on the chamber type and the
        locations of detected arena wells.

        Arguments:
          - img: the image to segment.
          - centers: list of detected wells for the image (each center is a tuple
                     ordered pair of X and Y coordinates)
          - avgDists: tuple list of the average distances along X and Y direction
                      between neighboring wells
          - numRowsCols: tuple list of the number of rows and columns of wells.

        Returns:
          - sortedBBoxes: list of the bounding boxes for each sub-image
        """
        bboxes = []
        self.getPixelToMMRatio()
        pxToMM = self.pxToMM
        if self.ct is CT.large.name:
            bboxes = self.getLargeChamberBBoxesAndImages(centers, pxToMM)
        else:
            for center in centers:
                if (self.ct is CT.opto.name and not self.inverted) or (
                    self.ct is not CT.opto.name and self.inverted
                ):
                    bboxes.append(
                        [
                            max(center[0] - int(0.5 * avgDists[0]), 0),
                            max(center[1] - int(8.5 * pxToMM), 0),
                        ]
                    )
                    bboxes[-1] += [
                        center[0] + int(0.5 * avgDists[0]) - bboxes[-1][0],
                        center[1] - int(4 * pxToMM) - bboxes[-1][1],
                    ]
                    bboxes.append(
                        [
                            max(center[0] - int(0.5 * avgDists[0]), 0),
                            max(center[1] + int(4 * pxToMM), 0),
                        ]
                    )
                    bboxes[-1] += [
                        center[0] + int(0.5 * avgDists[0]) - bboxes[-1][0],
                        center[1] + int(8.5 * pxToMM) - bboxes[-1][1],
                    ]
                elif (self.ct is CT.opto.name and self.inverted) or (
                    self.ct is not CT.opto.name and not self.inverted
                ):
                    bboxes.append(
                        [
                            max(center[0] - int(8.5 * pxToMM), 0),
                            max(center[1] - int(0.5 * avgDists[1]), 0),
                        ]
                    )
                    bboxes[-1] += [
                        center[0] - int(4 * pxToMM) - bboxes[-1][0],
                        center[1] + int(0.5 * avgDists[1]) - bboxes[-1][1],
                    ]
                    bboxes.append(
                        [
                            max(center[0] + int(4 * pxToMM), 0),
                            max(center[1] - int(0.5 * avgDists[1]), 0),
                        ]
                    )
                    bboxes[-1] += [
                        center[0] + int(8.5 * pxToMM) - bboxes[-1][0],
                        center[1] + int(0.5 * avgDists[1]) - bboxes[-1][1],
                    ]
        if self.ct is CT.opto.name:
            return CT.opto.value().getSortedBBoxes(bboxes)
        sortedBBoxes = []
        for j in range(numRowsCols[1 if self.inverted else 0]):
            for i in range(numRowsCols[0 if self.inverted else 1]):
                offset = 4 if self.ct is CT.large.name else 2
                idx = numRowsCols[1 if self.inverted else 0] * offset * i + offset * j
                sortedBBoxes.append(bboxes[idx])
                sortedBBoxes.append(bboxes[idx + 1])
                if self.ct is CT.large.name:
                    for k in range(2, 4):
                        sortedBBoxes.append(bboxes[idx + k])
        return sortedBBoxes

    def processDetections(self):
        """
        Consolidate the arena well detections by organizing their X and Y
        coordinates into histograms and finding the bins with local maxima.

        For all chamber types excluding 4-circle, interpolate any missing detections
        using linear regressions.

        Check if the image is skewed, and flag it accordingly.
        """
        self.wellCoords = [[], []]
        self.detections = [
            np.asarray([centroid[i] for centroid in self.centroids]) for i in range(2)
        ]
        histResults = [
            binned_statistic(detection_set, [], bins=40, statistic="count")
            for detection_set in self.detections
        ]
        binHtsOrig = [res.statistic for res in histResults]
        binClusters = [trueRegions(ht_set > 0) for ht_set in binHtsOrig]
        use_bin_ht_test = any(len(cl) > 3 for cl in binClusters)

        for detI, det_set in enumerate(self.detections):
            hts = histResults[detI].statistic
            clusters = binClusters[detI]
            for idx in (0, -1):
                if not use_bin_ht_test:
                    continue
                if len(clusters) > 0 and np.sum(hts[clusters[idx]]) == 1:
                    del clusters[idx]

            for i, trueRegion in enumerate(clusters):
                self.wellCoords[detI].append(
                    int(
                        round(
                            np.mean(
                                [
                                    det_set[
                                        (
                                            histResults[detI].binnumber - 1
                                            >= trueRegion.start
                                        )
                                        & (
                                            histResults[detI].binnumber
                                            <= trueRegion.stop
                                        )
                                    ]
                                ]
                            )
                        )
                    )
                )
            for i, coord_set in enumerate(self.wellCoords):
                new_coord_set = []
                for c in coord_set:
                    closest_dist_to_edge = min(
                        c, self.img_shape_resized[0 if i else 1] - c
                    )
                    if closest_dist_to_edge >= 10:
                        new_coord_set.append(c)
                self.wellCoords[i] = (
                    new_coord_set  # First pass: apply boundary filtering
                )

        # Second pass: apply sorting and outlier rejection separately
        for i in range(len(self.wellCoords)):
            self.wellCoords[i] = sorted(self.wellCoords[i])
            self.wellCoords[i] = reject_outliers_by_delta(
                np.asarray(self.wellCoords[i])
            )

        wells = list(itertools.product(self.wellCoords[0], self.wellCoords[1]))
        self.numRowsCols = [len(self.wellCoords[i]) for i in range(1, -1, -1)]
        self.ct, self.inverted = getChamberTypeByRowsAndCols(self.numRowsCols)
        diagDist = distance((0, 0), self.img_shape_resized[:2])

        for centroid in list(self.centroids):
            closestWell = min(wells, key=lambda xy: distance(xy, centroid))
            if distance(closestWell, centroid) > 0.02 * diagDist:
                self.centroids.remove(centroid)

        self.sortedCentroids = []
        for well in wells:
            closestDetection = min(self.centroids, key=lambda xy: distance(xy, well))
            if distance(closestDetection, well) > 0.02 * diagDist:
                self.sortedCentroids.append((np.nan, np.nan))
            else:
                self.sortedCentroids.append(closestDetection)

        self.sortedCentroids = np.array(self.sortedCentroids).reshape(
            tuple(reversed(self.numRowsCols))
            + (() if None in self.sortedCentroids else (-1,))
        )
        sc = self.sortedCentroids
        mask = np.isnan(np.squeeze(sc[:, :, 0]))
        sec = np.where(mask.all(0))
        third = np.where(mask.all(1))
        sc = np.delete(np.delete(sc, sec, 1), third, 0)
        self.sortedCentroids = sc

        self.rowRegressions = np.zeros(self.sortedCentroids.shape[1], dtype=object)
        self.colRegressions = np.zeros(self.sortedCentroids.shape[0], dtype=object)
        self.interpolateCentroids()

        prelim_corners = fake_image_corners(self.sortedCentroids)
        true_corners = corners(self.sortedCentroids, prelim_corners)
        width_skew = abs(
            distance(true_corners["tr"], true_corners["tl"])
            - distance(true_corners["br"], true_corners["bl"])
        )
        height_skew = abs(
            distance(true_corners["br"], true_corners["tr"])
            - distance(true_corners["bl"], true_corners["tl"])
        )
        self.skewed = (
            height_skew / self.img_shape_resized[0] > 0.01
            or width_skew / self.img_shape_resized[1] > 0.01
        )

        if self.skewed and not self.allowSkew:
            print(
                f"Warning: skew detected in image {self.img_name}. To analyze "
                "this image, use flag --allowSkew."
            )
            currentThread = threading.currentThread()
            setattr(currentThread, "hadError", True)

    def interpolateCentroids(self):
        """Find any centroids with NaN coordinates and interpolate their positions
        based on neighbors in their row and column.
        """
        for i, col in enumerate(self.sortedCentroids):
            for j, centroid in enumerate(col):
                row = self.sortedCentroids[:, j]
                regResult = calculateRegressions(row, col)[0]
                if j == 0:
                    self.colRegressions[i] = regResult["col"]
                if i == 0:
                    self.rowRegressions[j] = regResult["row"]
                if len(centroid[np.isnan(centroid)]):
                    row = self.sortedCentroids[:, j]
                    self.sortedCentroids[i, j] = linearIntersection(
                        dict(row=self.rowRegressions[j], col=self.colRegressions[i])
                    )

    def resize_image_shape(self):
        self.img_shape_resized = [
            dim * self.predict_resize_factor for dim in self.img_shape
        ]

    def resize_image(self):
        self.imageResized = cv2.resize(
            self.img,
            (0, 0),
            fx=self.predict_resize_factor,
            fy=self.predict_resize_factor,
            interpolation=cv2.INTER_CUBIC,
        )
        image = normalize(self.imageResized, 1, 99.8, axis=(0, 1))
        self.imageResized = image.astype(np.float32)

    def findCircles(self, debug=False, predictions=None, include_img=False):
        """Find the location of arena wells for the image in attribute `self.img`.

        Arguments:
          - debug: if True, displays the inputted image with markers over detected
                   landmarks and prints their coordinates to the console
          - predictions: positions of predicted landmarks. Note: its format must match
                     that of the second element of the tuple returned from a call to
                     predict_instances method of a SplineDist model. Defaults to None,
                     in which case new predictions are made.

        Returns:
          - wells: list of the coordinates of detected wells.
          - avgDists: tuple list of the average distances along X and Y direction
                      between neighboring wells.
          - numRowsCols: tuple list of the number of rows and columns of wells.
          - rotatedImg: `self.img` after being rotated to best align rows and
                        columns with the border of the image.
          - rotationAngle: angle in radians by which the image was rotated.
        """
        self.resize_image_shape()
        if include_img:
            self.resize_image()

        # If no predictions given, run the model
        if predictions is None:
            _, predictions = self.model.predict_instances(self.imageResized)

        # ================================
        #  Fit circles from outlines
        # ================================
        self.centroids = []
        if "outlines" in predictions:
            for outline in predictions["outlines"]:
                outline_arr = np.array(outline)
                cx, cy, r = fit_circle_kasa(outline_arr)
                self.centroids.append((cy, cx))
        if debug:
            print("what are centroids?", self.centroids)
            if not hasattr(self, "imageResized"):
                with app.app_context():
                    self.imageResized = byte_to_bgr(
                        EggLayingImage.query.filter_by(
                            session_id=self.room, basename=self.img_name
                        )
                        .first()
                        .image
                    )
                    self.imageResized = cv2.resize(
                        self.imageResized,
                        (0, 0),
                        fx=self.predict_resize_factor,
                        fy=self.predict_resize_factor,
                    )
            imgCopy = cv2.resize(np.array(self.imageResized), (0, 0), fx=0.5, fy=0.5)
            for centroid in self.centroids:
                cv2.drawMarker(
                    imgCopy,
                    tuple([int(el * 0.5) for el in centroid]),
                    COL_G,
                    cv2.MARKER_TRIANGLE_UP,
                )
            cv2.imshow(
                f"debug/{self.img_name}", cv2.cvtColor(imgCopy, cv2.COLOR_RGB2BGR)
            )
            cv2.waitKey(0)
        self.processDetections()
        rotationAngle = 0
        if include_img:
            rotatedImg = self.img
        image_origin = tuple(np.array(self.img_shape_resized[1::-1]) / 2)
        if self.ct is not CT.large.name:
            rotationAngle = 0.5 * (
                math.atan(np.mean([el["slope"] for el in self.rowRegressions]))
                - math.atan(np.mean([1 / el["slope"] for el in self.colRegressions]))
            )
            if include_img:
                rotatedImg = rotate_image(self.img, rotationAngle)
            for i, centroid in enumerate(self.centroids):
                self.centroids[i] = rotate_around_point_highperf(
                    centroid, rotationAngle, image_origin
                )
        self.processDetections()
        wells = np.array(
            [
                np.round(np.divide(well, self.predict_resize_factor)).astype(int)
                for well in self.sortedCentroids
            ]
        )
        for i in range(len(self.wellCoords)):
            self.wellCoords[i] = np.round(
                np.divide(self.wellCoords[i], self.predict_resize_factor)
            ).astype(int)
        self.avgDists = [np.mean(np.diff(self.wellCoords[i])) for i in range(2)]
        if self.inverted:
            wells = np.transpose(wells, (1, 0, 2))
        wells = wells.reshape(self.numRowsCols[0] * self.numRowsCols[1], 2).astype(int)
        self.wells = wells
        self.rotationAngle = rotationAngle

        return [
            wells,
            self.avgDists,
            self.numRowsCols,
            self.rotationAngle,
            rotatedImg if include_img else None,
        ]


def rotate_image(image, angle):
    """Rotate image by an inputted angle.

    Arguments:
      - image: the image to rotate.
      - angle: the degree to which to rotate (in radians).
    """
    image_center = tuple(np.array(image.shape[1::-1]) / 2)
    rot_mat = cv2.getRotationMatrix2D(image_center, 180 * angle / math.pi, 1.0)
    result = cv2.warpAffine(image, rot_mat, image.shape[1::-1], flags=cv2.INTER_LINEAR)
    return result


def rotate_around_point_highperf(xy, radians, origin=(0, 0)):
    """Rotate a point around a given point.
    source: https://gist.github.com/LyleScott/e36e08bfb23b1f87af68c9051f985302

    Arguments:
      - xy: tuple containing two Numpy arrays of the X and Y coordinates of
            points to rotate
      - radians: angle in radians by which to rotate the points
      - origin: origin about which to rotate the points (default: (0, 0))
    """
    x, y = xy
    offset_x, offset_y = origin
    adjusted_x = x - offset_x
    adjusted_y = y - offset_y
    cos_rad = math.cos(radians)
    sin_rad = math.sin(radians)
    qx = offset_x + cos_rad * adjusted_x + sin_rad * adjusted_y
    qy = offset_y + -sin_rad * adjusted_x + cos_rad * adjusted_y
    return [qx, qy]


def calculateRegressions(row, col):
    """Calculate linear regressions for the Y values in the list of points in a
    given column and the X values in the list of points in a given row.

    Arguments:
      - row: list of points (XY tuples) in a row of interest
      - col: list of points (XY tuples) in a column of interest

    Returns:
      - regressions: dictionary containing regression slopes and intercepts
      - row_residuals: list of absolute differences between the actual and predicted Y values
      - col_residuals: list of absolute differences between the actual and predicted X values
    """
    colModel = LinearRegression()
    colInd = np.array([el[1] for el in col if not np.isnan(el).any()]).reshape(-1, 1)
    colActual = np.array([el[0] for el in col if not np.isnan(el).any()])
    colModel.fit(colInd, colActual)
    colPredicted = colModel.predict(colInd)
    colResiduals = np.abs(colActual - colPredicted)

    rowModel = LinearRegression()
    rowInd = np.array([el[0] for el in row if not np.isnan(el).any()]).reshape(-1, 1)
    rowActual = np.array([el[1] for el in row if not np.isnan(el).any()])
    rowModel.fit(rowInd, rowActual)
    rowPredicted = rowModel.predict(rowInd)
    rowResiduals = np.abs(rowActual - rowPredicted)

    if colModel.coef_[0] == 0:
        colSlope = 1e-6
    else:
        colSlope = colModel.coef_[0]
    a = 1 / colSlope
    c = -colModel.intercept_ / colSlope
    b = rowModel.coef_[0]
    d = rowModel.intercept_

    return (
        dict(row=dict(slope=b, intercept=d), col=dict(slope=a, intercept=c)),
        rowResiduals,
        colResiduals,
    )


def linearIntersection(regressions):
    """Calculate the intersection of two linear regressions.

    Arguments:
      - regression: dictionary of the form
      {'row': {'slope': number, 'intercept': number},
       'col': {'slope': number, 'intercept': number}}
    """
    r = regressions
    interceptDiff = r["row"]["intercept"] - r["col"]["intercept"]
    slopeDiff = r["col"]["slope"] - r["row"]["slope"]
    return (
        (interceptDiff) / (slopeDiff),
        r["col"]["slope"] * interceptDiff / slopeDiff + r["col"]["intercept"],
    )


def reject_outliers_by_delta(binCenters, m=1.3):
    """Reject outliers based on the magnitude of their difference from neighboring
    points.

    Arguments:
      - binCenters: 1D Numpy array of values to check
      - m: sensitivity of the outlier test, smaller for more sensitivity
          (default: 1.3)
    """
    diffs = np.diff(binCenters)
    outIdxs = list(range(len(binCenters)))
    delta_mags = abs(diffs - np.mean(diffs))
    if np.all(delta_mags < 3):
        return binCenters
    idxs = np.squeeze(np.argwhere(~(delta_mags < m * np.std(diffs))))
    if idxs.shape == ():
        idxs = np.reshape(idxs, 1)
    for idx in idxs:
        if idx == 0:
            idxToRemove = idx
        elif idx == len(binCenters) - 2:
            idxToRemove = idx + 1
        if np.mean(np.delete(diffs, idx)) * 1.5 > diffs[idx]:
            continue
        if "idxToRemove" in locals() and idxToRemove in outIdxs:
            outIdxs.remove(idxToRemove)
    return binCenters[outIdxs]
