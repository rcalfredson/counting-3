import cv2

from project.lib.image.circleFinder import CircleFinder, rotate_image
from project.lib.image.node_based_segmenter import NodeBasedSegmenter


class SubImageHelper:
    def get_sub_images(self, img, img_path, alignment_data, room):
        if "nodes" in alignment_data:

            self.segment_image_via_alignment_data(img, img_path, alignment_data, room)
        elif "bboxes" in alignment_data:
            self.segment_image_via_bboxes(img, alignment_data)

    def segment_image_via_alignment_data(self, img, img_path, alignment_data, room):
        segmenter = NodeBasedSegmenter(
            img,
            img_path,
            alignment_data["nodes"],
            alignment_data["type"],
            alignment_data.get("inverted", False),
            room,
        )
        self.subImgs, self.bboxes = segmenter.calc_bboxes_and_subimgs()
        self.rotation_angle = segmenter.rotation_angle

    def segment_image_via_bboxes(self, img, alignment_data):
        img = cv2.resize(
            img,
            (0, 0),
            fx=alignment_data.get("scaling", 1),
            fy=alignment_data.get("scaling", 1),
        )
        img = rotate_image(img, alignment_data["rotationAngle"])
        self.bboxes = alignment_data["bboxes"]
        bbox_translation = [
            -el for el in alignment_data.get("imageTranslation", [0, 0])
        ]
        alignment_data["regionsToIgnore"] = []
        translated_bboxes = []
        for bbox in self.bboxes:
            new_bbox = [
                bbox[0] + bbox_translation[0],
                bbox[1] + bbox_translation[1],
                bbox[2],
                bbox[3],
            ]
            if new_bbox[0] < 0:
                new_bbox[2] += new_bbox[0]
                new_bbox[0] = 0
            if new_bbox[1] < 0:
                new_bbox[2] += new_bbox[1]
                new_bbox[1] = 0
            translated_bboxes.append(list(map(round, new_bbox)))

        self.rotation_angle = alignment_data["rotationAngle"]
        self.bboxes = translated_bboxes
        self.subImgs = CircleFinder.getSubImagesFromBBoxes(
            img, translated_bboxes, alignment_data["regionsToIgnore"]
        )
