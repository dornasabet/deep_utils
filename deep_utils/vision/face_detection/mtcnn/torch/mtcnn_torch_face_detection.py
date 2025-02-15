import numpy as np
from deep_utils.vision.face_detection.main.main_face_detection import FaceDetector
from deep_utils.utils.lib_utils.lib_decorators import get_from_config, expand_input, get_elapsed_time, rgb2bgr
from deep_utils.utils.lib_utils.download_utils import download_decorator
from deep_utils.utils.box_utils.boxes import Box, Point
from .config import Config
from .src.get_nets import get_nets
from .src.box_utils import nms, calibrate_box, get_image_boxes, convert_to_square
from .src.first_stage import run_first_stage


class MTCNNTorchFaceDetector(FaceDetector):
    def __init__(self, **kwargs):
        super().__init__(name=self.__class__.__name__,
                         file_path=__file__,
                         download_variables=("pnet", "onet", 'rnet'),
                         **kwargs)
        self.config: Config

    @download_decorator
    def load_model(self):
        # LOAD MODELS
        PNet, RNet, ONet = get_nets()
        pnet = PNet(self.config.pnet).to(self.config.device)
        rnet = RNet(self.config.rnet).to(self.config.device)
        onet = ONet(self.config.onet).to(self.config.device)
        pnet.eval()
        rnet.eval()
        onet.eval()
        self.model = dict(pnet=pnet, rnet=rnet, onet=onet)

    @rgb2bgr('rgb')
    @get_elapsed_time
    @expand_input(3)
    @get_from_config
    def detect_faces(self,
                     img,
                     is_rgb,
                     min_face_size=None,
                     thresholds=None,
                     nms_thresholds=None,
                     min_detection_size=None,
                     factor=None,
                     confidence=None,
                     round_prec=4,
                     get_time=False):
        import torch
        # BUILD AN IMAGE PYRAMID
        width, height = img.shape[1:3]
        min_length = min(height, width)

        # scales for scaling the image
        scales = []

        # scales the image so that
        # minimum size that we can detect equals to
        # minimum face size that we want to detect
        m = min_detection_size / min_face_size
        min_length *= m

        factor_count = 0
        while min_length > min_detection_size:
            scales.append(m * factor ** factor_count)
            min_length *= factor
            factor_count += 1

        # STAGE 1

        # it will be returned
        bounding_boxes = []

        # run P-Net on different scales
        for s in scales:
            boxes = run_first_stage(img,
                                    self.model["pnet"],
                                    scale=s,
                                    threshold=thresholds[0],
                                    device=self.config.device)
            bounding_boxes.append(boxes)
        bounding_boxes_ = bounding_boxes
        image_boxes = []
        join_bounding_boxes = []
        for img_n in range(img.shape[0]):
            bounding_boxes = [box[img_n] for box in bounding_boxes_ if box is not None]
            # bounding_boxes = [i for i in bounding_boxes if i is not None]
            if len(bounding_boxes) == 0:
                continue
            bounding_boxes = np.vstack(bounding_boxes)

            keep = nms(bounding_boxes[:, 0:5], nms_thresholds[0])
            bounding_boxes = bounding_boxes[keep]

            # use offsets predicted by pnet to transform bounding boxes
            bounding_boxes = calibrate_box(bounding_boxes[:, 0:5], bounding_boxes[:, 5:])
            # shape [n_boxes, 5]

            bounding_boxes = convert_to_square(bounding_boxes)
            bounding_boxes[:, 0:4] = np.round(bounding_boxes[:, 0:4])

            # STAGE 2

            img_boxes = get_image_boxes(bounding_boxes, img[img_n], size=24)
            if img_boxes.size != 0:
                image_boxes.append(img_boxes)
                join_bounding_boxes.append(bounding_boxes)
        if len(image_boxes) == 0:
            return dict(boxes=[], confidences=[], landmarks=[])
        bounding_boxes_ = np.vstack(join_bounding_boxes)
        split = [0]
        for img_box in image_boxes:
            split.append(img_box.shape[0] + split[-1])
        img_boxes = torch.FloatTensor(np.concatenate(image_boxes)).to(self.config.device)
        output = self.model["rnet"](img_boxes)
        image_boxes = []
        join_bounding_boxes = []
        for img_n in range(img.shape[0]):
            offsets = output[0].cpu().data.numpy()[split[img_n]: split[img_n + 1]]  # shape [n_boxes, 4]
            probs = output[1].cpu().data.numpy()[split[img_n]: split[img_n + 1]]  # shape [n_boxes, 2]
            bounding_boxes = bounding_boxes_[split[img_n]: split[img_n + 1]]

            keep = np.where(probs[:, 1] > thresholds[1])[0]
            bounding_boxes = bounding_boxes[keep]
            bounding_boxes[:, 4] = probs[keep, 1].reshape((-1,))
            offsets = offsets[keep]

            keep = nms(bounding_boxes, nms_thresholds[1])
            bounding_boxes = bounding_boxes[keep]
            bounding_boxes = calibrate_box(bounding_boxes, offsets[keep])
            bounding_boxes = convert_to_square(bounding_boxes)
            bounding_boxes[:, 0:4] = np.round(bounding_boxes[:, 0:4])

            # STAGE 3

            img_boxes = get_image_boxes(bounding_boxes, img[img_n], size=48)
            if img_boxes.size != 0:
                image_boxes.append(img_boxes)
                join_bounding_boxes.append(bounding_boxes)
        if len(image_boxes) == 0:
            return dict(boxes=[], confidences=[], landmarks=[])
        split = [0]
        for img_box in image_boxes:
            split.append(img_box.shape[0] + split[-1])
        img_boxes = np.vstack(image_boxes)
        bounding_boxes_ = np.vstack(join_bounding_boxes)
        img_boxes = torch.FloatTensor(img_boxes).to(self.config.device)
        output = self.model["onet"](img_boxes)
        boxes_, confidences_, landmarks_ = [], [], []
        face_points = ["left_eye", "right_eye", "nose", "mouth_left", "mouth_right"]
        for img_n in range(img.shape[0]):
            bounding_boxes = bounding_boxes_[split[img_n]: split[img_n + 1]]
            landmarks = output[0].cpu().data.numpy()[split[img_n]: split[img_n + 1]]
            offsets = output[1].cpu().data.numpy()[split[img_n]: split[img_n + 1]]  # shape [n_boxes, 4]
            probs = output[2].cpu().data.numpy()[split[img_n]: split[img_n + 1]]  # shape [n_boxes, 2]
            keep = np.where(probs[:, 1] > thresholds[2])[0]
            bounding_boxes = bounding_boxes[keep]
            bounding_boxes[:, 4] = probs[keep, 1].reshape((-1,))
            offsets = offsets[keep]
            landmarks = landmarks[keep]

            width = bounding_boxes[:, 2] - bounding_boxes[:, 0] + 1.0
            height = bounding_boxes[:, 3] - bounding_boxes[:, 1] + 1.0
            xmin, ymin = bounding_boxes[:, 0], bounding_boxes[:, 1]
            landmarks[:, 0:5] = np.expand_dims(xmin, 1) + np.expand_dims(width, 1) * landmarks[:, 0:5]
            landmarks[:, 5:10] = np.expand_dims(ymin, 1) + np.expand_dims(height, 1) * landmarks[:, 5:10]

            bounding_boxes = calibrate_box(bounding_boxes, offsets)
            keep = nms(bounding_boxes, nms_thresholds[2], mode='min')
            landmarks = landmarks[keep]
            boxes, confidences = bounding_boxes[keep][:, :4], bounding_boxes[keep][:, 4]
            keep = confidences >= confidence
            boxes, confidences, landmarks = boxes[keep], confidences[keep], landmarks[keep]
            boxes = Box.box2box(boxes, in_source=Box.BoxSource.Torch, to_source=Box.BoxSource.Numpy)
            boxes_.append(boxes)
            confidences_.append(confidences.round(round_prec))
            if len(landmarks) != 0:
                landmarks = [[Point.point2point((landmarks[j][i], landmarks[j][5 + i]),
                                                in_source='Torch', to_source='Numpy') for i in range(5)] for j in
                             range(landmarks.shape[0])]
            img_landmarks = []
            for i in range(len(landmarks)):
                face_dict = {}
                for points, face in zip(landmarks[i], face_points):
                    face_dict[face] = points
                img_landmarks.append(face_dict)
            landmarks_.append(img_landmarks)
        output = self.output_class(boxes=boxes_, confidences=confidences_, landmarks=landmarks_)
        return output
