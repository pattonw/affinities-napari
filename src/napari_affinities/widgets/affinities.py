# local package imports
from ..gp.pipeline import build_pipeline
from .gui_helpers import layer_choice_widget
from ..bioimageio.helpers import get_torch_module

# github repo libraries
import gunpowder as gp

# pip installed libraries
import napari
from napari.qt.threading import thread_worker
from magicgui.widgets import create_widget, Container
import bioimageio.core
from bioimageio.core.resource_io.nodes import Model
from bioimageio.core.prediction_pipeline import create_prediction_pipeline
from marshmallow import missing
import torch
import numpy as np
from xarray import DataArray
from superqt import QCollapsible
from qtpy.QtWidgets import (
    QWidget,
    QHBoxLayout,
    QVBoxLayout,
    QPushButton,
    QFileDialog,
    QInputDialog,
    QLabel,
    QFrame,
)

# python built in libraries
from pathlib import Path
from typing import Optional, Dict, List
from contextlib import contextmanager


class ModelWidget(QWidget):
    def __init__(self, napari_viewer):
        # basic initialization
        self.viewer = napari_viewer
        super().__init__()

        # initialize state variables
        self.__training_generator = None

        # supported axes
        self.__axes = ["batch", "channel", "time", "z", "y", "x"]

        # Widget layout
        layout = QVBoxLayout()

        # Model name Label Widget
        self.model_label = QLabel()
        layout.addWidget(self.model_label)

        # Load model from file Widget
        model_file_loader = QPushButton("Load a model from File!", self)
        model_file_loader.clicked.connect(self.model_from_file)
        layout.addWidget(model_file_loader)

        # Load model from url Widget
        model_url_loader = QPushButton("Load a model from Url!", self)
        model_url_loader.clicked.connect(self.model_from_url)
        layout.addWidget(model_url_loader)

        # Save model to file
        self.save_button = QPushButton("Save model!", self)
        self.save_button.clicked.connect(self.save_model)
        layout.addWidget(self.save_button)

        # Train widget(Collapsable)
        collapsable_train_widget = QCollapsible("Training: expand for options:", self)
        self.train_widget = self.create_train_widget(napari_viewer)
        collapsable_train_widget.addWidget(
            self.train_widget.native
        )  # FunctionGui -> QWidget via .native

        # add loss/iterations widget
        iterations_frame = QFrame(collapsable_train_widget)
        iterations_layout = QHBoxLayout()
        iterations_label = QLabel("iterations:")
        self.iterations_widget = QLabel("None")
        iterations_layout.addWidget(iterations_label)
        iterations_layout.addWidget(self.iterations_widget)
        iterations_frame.setLayout(iterations_layout)
        collapsable_train_widget.addWidget(iterations_frame)

        loss_frame = QFrame(collapsable_train_widget)
        loss_layout = QHBoxLayout()
        loss_label = QLabel("loss:")
        self.loss_widget = QLabel("nan")
        loss_layout.addWidget(loss_label)
        loss_layout.addWidget(self.loss_widget)
        loss_frame.setLayout(loss_layout)
        collapsable_train_widget.addWidget(loss_frame)

        # add buttons
        self.reset_training_state()
        self.train_button = QPushButton("Train!", self)
        self.train_button.clicked.connect(self.train)
        self.pause_button = QPushButton("Pause!", self)
        self.pause_button.clicked.connect(self.pause_training)
        self.snapshot_button = QPushButton("Snapshot!", self)
        self.snapshot_button.clicked.connect(self.snapshot)
        collapsable_train_widget.addWidget(self.train_button)
        collapsable_train_widget.addWidget(self.pause_button)
        collapsable_train_widget.addWidget(self.snapshot_button)

        layout.addWidget(collapsable_train_widget)

        # Predict widget(Collapsable)
        collapsable_predict_widget = QCollapsible(
            "Prediction: expand for options:", self
        )
        self.predict_widget = self.create_predict_widget(napari_viewer)
        collapsable_predict_widget.addWidget(self.predict_widget.native)

        # add buttons
        self.predict_button = QPushButton("Predict!", self)
        self.predict_button.clicked.connect(self.predict)
        collapsable_predict_widget.addWidget(self.predict_button)

        layout.addWidget(collapsable_predict_widget)

        # activate layout
        self.setLayout(layout)

        # Widget state
        self.model = None

        # No buttons should be enabled
        self.enable_buttons()

    @property
    def model(self) -> Optional[Model]:
        return self.__model

    @contextmanager
    def build_pipeline(self, raw, gt, mask, lsds):
        with build_pipeline(raw, gt, mask, lsds, self.model) as pipeline:
            yield pipeline

    def reset_training_state(self):
        if self.__training_generator is not None:
            self.__training_generator.quit()
        self.__training_generator = None
        self.iterations_widget.setText("None")
        self.loss_widget.setText("nan")

    def enable_buttons(
        self,
        train: bool = False,
        pause: bool = False,
        snapshot: bool = False,
        predict: bool = False,
        save: bool = False,
    ):
        self.train_button.setEnabled(train)
        self.pause_button.setEnabled(pause)
        self.snapshot_button.setEnabled(snapshot)
        self.predict_button.setEnabled(predict)
        self.save_button.setEnabled(save)

    @model.setter
    def model(self, new_model: Optional[Model]):
        self.reset_training_state()
        self.__model = new_model
        if new_model is not None:
            self.model_label.setText(new_model.name)
            self.enable_buttons(train=True, predict=True, snapshot=True, save=True)
        else:
            self.model_label.setText("None")

    def start_training_loop(self):
        self.__training_generator = self.train_affinities(
            self.train_widget.raw.value,
            self.train_widget.gt.value,
            self.train_widget.mask.value,
            self.train_widget.lsds.value,
        )
        self.__training_generator.yielded.connect(self.on_yield)
        self.__training_generator.start()

    def train(self):
        if self.__training_generator is None:
            self.start_training_loop()
        else:
            self.__training_generator.resume()
        self.enable_buttons(pause=True, predict=True, snapshot=True, save=True)

    def pause_training(self):
        self.__training_generator.pause()
        self.enable_buttons(train=True, predict=True, save=True, snapshot=True)

    def snapshot(self):
        if self.__training_generator is None:
            self.train()
        self.__training_generator.send("snapshot")
        self.__training_generator.resume()
        self.enable_buttons(pause=True, predict=True, snapshot=True, save=True)

    def predict(self):
        if self.__training_generator is None:
            self.train()
        self.__training_generator.send(
            self.train_widget.raw.value.data,
        )
        self.__training_generator.resume()
        self.enable_buttons(pause=True, predict=True, snapshot=True, save=True)

    def create_train_widget(self, viewer):
        # inputs:
        raw = layer_choice_widget(
            viewer,
            annotation=napari.layers.Image,
            name="raw",
        )
        gt = layer_choice_widget(viewer, annotation=napari.layers.Labels, name="gt")
        mask = layer_choice_widget(
            viewer, annotation=Optional[napari.layers.Labels], name="mask"
        )
        lsds = create_widget(
            annotation=bool,
            name="lsds",
            label='<a href="https://localshapedescriptors.github.io"><font color=white>LSDs</font></a>',
            value=False,
        )

        train_widget = Container(widgets=[raw, gt, mask, lsds])

        return train_widget

    def create_predict_widget(self, viewer):
        # inputs:
        raw = layer_choice_widget(
            viewer,
            annotation=napari.types.ImageData,
            name="raw",
        )

        predict_widget = Container(widgets=[raw])

        return predict_widget

    def model_from_file(self):
        dlg = QFileDialog()
        dlg.setFileMode(QFileDialog.FileMode.ExistingFile)
        dlg.setNameFilters(["zip files (*.zip)"])
        dlg.selectNameFilter("zip files (*.zip)")

        if dlg.exec_():
            filenames = dlg.selectedFiles()
            model_file = Path(filenames[0])

            self.model = bioimageio.core.load_resource_description(model_file)

    def model_from_url(self):
        # TODO: async
        url, ok = QInputDialog.getText(
            self, "Url Input Dialog", "Url for model rdf.yaml folder:"
        )
        if ok:
            self.model = bioimageio.core.load_resource_description(url)

    def on_yield(self, step_data):
        iteration, loss, *layers = step_data
        if len(layers) > 0:
            self.add_layers(layers)
        if iteration is not None and loss is not None:
            self.iterations_widget.setText(f"{iteration}")
            self.loss_widget.setText(f"{loss}")

    def add_layers(self, layers):
        viewer_axis_labels = self.viewer.dims.axis_labels

        for data, metadata, layer_type in layers:
            # then try to update the viewer layer with that name.
            name = metadata.pop("name")
            axes = metadata.pop("axes")

            # handle viewer axes if still default numerics
            # TODO: Support using xarray axis labels as soon as napari does
            if len(set(viewer_axis_labels).intersection(set(axes))) == 0:
                spatial_axes = [
                    axis for axis in axes if axis not in ["batch", "channel"]
                ]
                assert (
                    len(viewer_axis_labels) - len(spatial_axes) <= 1
                ), f"Viewer has axes: {viewer_axis_labels}, but we expect ((channels), {spatial_axes})"
                viewer_axis_labels = (
                    ("channels", *spatial_axes)
                    if len(viewer_axis_labels) > len(spatial_axes)
                    else spatial_axes
                )
                self.viewer.dims.axis_labels = viewer_axis_labels

            batch_dim = axes.index("batch") if "batch" in axes else -1
            sample_shape = data.shape[batch_dim + 1 :]

            try:
                # add to existing layer
                layer = self.viewer.layers[name]

                # concatenate along batch dimension
                layer.data = np.concatenate(
                    [
                        layer.data.reshape(*(-1, *sample_shape)),
                        data.reshape(-1, *sample_shape),
                    ],
                    axis=0,
                )

                # if make first dimension "batch" if it isn't
                if viewer_axis_labels[0] != "batch":
                    viewer_axis_labels = ("batch", *viewer_axis_labels)
                    self.viewer.dims.axis_labels = viewer_axis_labels

            except KeyError:  # layer not in the viewer
                # TODO: Support defining layer axes as soon as napari does
                if layer_type == "image":
                    self.viewer.add_image(data, name=name, **metadata)
                elif layer_type == "labels":
                    self.viewer.add_labels(data, name=name, **metadata)

    @thread_worker
    def train_affinities(self, raw, gt, mask, lsds) -> int:

        iteration = 0
        if self.model is None:
            raise ValueError("Please load a model either from your filesystem or a url")

        # constants
        offsets = self.model.config["mws"]["offsets"]
        ndim = len(offsets[0])
        spatial_axes = ["time", "z", "y", "x"][-ndim:]

        # extract torch model from bioimageio format
        torch_module = get_torch_module(self.model)

        # define Loss function and Optimizer TODO: make options available as choices?
        lsd_loss_func = torch.nn.MSELoss()
        # aff_loss_func = torch.nn.BCEWithLogitsLoss()
        aff_loss_func = torch.nn.MSELoss()
        optimizer = torch.optim.Adam(params=torch_module.parameters())

        # TODO: How to display profiling stats
        device = torch.device("cuda")
        torch_module = torch_module.to(device)
        torch_module.train()

        # Train loop:
        with self.build_pipeline(raw, gt, mask, lsds) as pipeline:
            iteration = 0
            loss = float("nan")
            mode = yield (iteration, loss)
            while True:
                if mode is None or mode == "snapshot":
                    predict = False
                    snapshot_iteration = mode is not None
                elif isinstance(mode, np.ndarray):
                    predict = True
                    pred_data = mode
                else:
                    raise Exception(f"Uknown mode: {mode}")

                if predict:
                    checkpoint = f"checkpoints/{iteration}"
                    torch.save(torch_module.state_dict(), Path(checkpoint))
                    self.model.weights["pytorch_state_dict"].source = Path(checkpoint)

                    # Assuming raw data comes in with a channel dim
                    # This doesn't have to be the case, in which case
                    # plugin will fail.
                    # TODO: How to determine axes of raw data. metadata?
                    # guess? simply make it fit what the model expects?

                    # add batch dimension
                    pred_data = pred_data.reshape((1, *pred_data.shape))

                    with create_prediction_pipeline(bioimageio_model=self.model) as pp:
                        # [0] to access first input array/output array
                        pred_data = DataArray(
                            pred_data, dims=tuple(pp.input_specs[0].axes)
                        )
                        affs = pp(pred_data)[0].values

                    # remove batch dimensions
                    pred_data = pred_data.squeeze()
                    affs = affs.squeeze()

                    # assert result is as expected
                    assert (
                        pred_data.ndim == ndim
                    ), f"Raw has dims: {pred_data.ndim}, but expected: {ndim}"
                    assert (
                        affs.ndim == ndim + 1
                    ), f"Affs have dims: {affs.ndim}, but expected: {ndim+1}"
                    assert affs.shape[0] == len(offsets), (
                        f"Number of affinity channels ({affs.shape[0]}) "
                        f"does not match number of offsets ({len(offsets)})"
                    )

                    # Generate affinities and keep the offsets as metadata
                    mode = yield (
                        None,
                        None,
                        (
                            affs,
                            {
                                "name": "Affinities",
                                "metadata": {"offsets": offsets},
                                "axes": (
                                    "channel",
                                    *spatial_axes,
                                ),
                            },
                            "image",
                        ),
                    )
                else:

                    # fetch data:
                    arrays, snapshot_arrays = pipeline.next(snapshot_iteration)
                    tensors = [
                        torch.as_tensor(array[0], device=device).float()
                        for array in arrays
                    ]
                    raw, aff_target, aff_mask, *lsd_arrays = tensors

                    optimizer.zero_grad()
                    if lsds:
                        lsd_target, lsd_mask = lsd_arrays

                        aff_pred, lsd_pred = torch_module(raw)

                        aff_loss = aff_loss_func(
                            aff_pred * aff_mask,
                            aff_target * aff_mask,
                        )
                        lsd_loss = lsd_loss_func(
                            lsd_pred * lsd_mask, lsd_target * lsd_mask
                        )
                        loss = aff_loss * lsd_loss
                    else:
                        aff_pred = torch_module(raw)
                        loss = aff_loss_func(
                            aff_pred * aff_mask,
                            aff_target * aff_mask,
                        )

                    loss.backward()
                    optimizer.step()
                    iteration += 1

                    if snapshot_iteration:
                        pred_arrays = []
                        pred_arrays.append(
                            (
                                aff_pred.detach().cpu().numpy(),
                                {
                                    "name": "sample_aff_pred",
                                    "axes": (
                                        "batch",
                                        "channel",
                                        *spatial_axes,
                                    ),
                                },
                                "image",
                            )
                        )
                        if lsds:
                            pred_arrays.append(
                                (
                                    lsd_pred.detach().cpu().numpy(),
                                    {
                                        "name": "sample_lsd_pred",
                                        "axes": (
                                            "batch",
                                            "channel",
                                            *spatial_axes,
                                        ),
                                    },
                                    "image",
                                )
                            )
                        mode = yield (
                            iteration,
                            loss,
                            *arrays,
                            *snapshot_arrays,
                            *pred_arrays,
                        )
                    else:
                        mode = yield (iteration, loss)
