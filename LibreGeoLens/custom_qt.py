import os
import json
import subprocess
from PIL import Image
from qgis.PyQt.QtGui import QPixmap, QImage, QColor
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (QMessageBox, QInputDialog, QLabel, QVBoxLayout, QPushButton, QWidget,
                                 QDialog, QScrollArea, QTextBrowser, QHBoxLayout)
from qgis.core import (QgsRectangle, QgsWkbTypes, QgsProject, QgsGeometry, QgsPointXY,
                       QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeatureRequest)
from qgis.gui import QgsMapToolEmitPoint, QgsRubberBand


def zoom_to_and_flash_feature(feature, canvas, layer):
    if not feature or not feature.geometry():
        raise ValueError("Invalid feature or geometry.")
    if not layer or not layer.crs():
        raise ValueError("Invalid layer or CRS.")

    # Create a rubber band to highlight the feature
    rubber_band = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
    rubber_band.setColor(QColor(255, 255, 0))  # Yellow outline
    rubber_band.setWidth(3)  # Outline width
    rubber_band.setFillColor(Qt.transparent)  # Transparent fill

    geometry = feature.geometry()

    # Retrieve the CRS from the layer and canvas
    crs_src = layer.crs()  # Source CRS from the layer
    crs_dest = canvas.mapSettings().destinationCrs()  # Destination CRS from the map canvas
    if crs_src != crs_dest:
        transform = QgsCoordinateTransform(crs_src, crs_dest, QgsProject.instance())
        geometry.transform(transform)

    # Add geometry to the rubber band
    rubber_band.setToGeometry(geometry, None)
    rubber_band.show()

    # Get the current and feature's extents
    current_extent = canvas.extent()
    feature_extent = geometry.boundingBox()
    # If the feature is entirely outside the current view or not fully visible
    if not current_extent.contains(feature_extent):
        # Calculate necessary zoom to fit the feature
        canvas.setExtent(feature_extent.buffered(feature_extent.width() * 0.1))  # Add 10% buffer for better visibility
        canvas.refresh()

    # Remove highlight after 2 seconds
    QTimer.singleShot(2000, lambda: rubber_band.reset(QgsWkbTypes.PolygonGeometry))


class CustomTextBrowser(QTextBrowser):
    def __init__(self, parent=None):
        super().__init__(parent)

    def setSource(self, url):
        """Override setSource to prevent navigation."""
        if url.scheme() == "image":
            # Ignore navigation for custom image links
            return
        super().setSource(url)  # Call parent method for other links


class ImageDisplayWidget(QWidget):
    def __init__(self, parent=None, canvas=None, log_layer=None):
        super().__init__(parent)
        self.canvas = canvas
        self.log_layer = log_layer
        self.image_dialog = None
        self.images = []

        # So that a double click does not also trigger a single click
        self.single_click_timer = QTimer(self)
        self.single_click_timer.setSingleShot(True)
        self.single_click_timer.timeout.connect(self.perform_single_click_action)
        self.current_image_metadata = None

        # Create the main layout for the widget
        self.layout = QVBoxLayout(self)

        # Create a horizontal scroll area
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        # Create a container widget for the images
        self.image_container = QWidget()
        self.image_layout = QHBoxLayout(self.image_container)
        self.image_layout.setContentsMargins(0, 0, 0, 0)  # Remove margins
        self.image_layout.setSpacing(5)  # Space between images

        # Set the container widget as the scroll area's widget
        self.scroll_area.setWidget(self.image_container)
        self.layout.addWidget(self.scroll_area)

    def add_image(self, image_path=None, image=None):
        """Set the image to be displayed"""
        assert (image_path is not None and image is None) or (image_path is None and image is not None)

        # Load and process the image
        if image_path is not None:
            image = Image.open(image_path)
            data = image.tobytes("raw", "RGBA")
            image = QImage(data, image.width, image.height, QImage.Format_RGBA8888)

        self.images.append({"image": image, "image_path": image_path, "chip_id": None, "rectangle_geom": None})

        # Create a widget to hold the image and its controls
        image_widget = QWidget(self.image_container)
        image_widget.setFixedSize(120, 120)  # Match the image size
        image_widget_layout = QVBoxLayout(image_widget)
        image_widget_layout.setContentsMargins(0, 0, 0, 0)
        image_widget_layout.setSpacing(0)

        # Create a container for the image and overlay controls
        image_container = QWidget(image_widget)
        image_container.setFixedSize(120, 120)
        image_container_layout = QVBoxLayout(image_container)
        image_container_layout.setContentsMargins(0, 0, 0, 0)
        image_container_layout.setSpacing(0)

        # Holds the actual image
        pixmap = QPixmap.fromImage(image)
        scaled_pixmap = pixmap.scaled(100, 100, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        label = QLabel(self)
        label.setPixmap(scaled_pixmap)
        label.setAlignment(Qt.AlignCenter)
        label.setFixedSize(120, 120)
        label.mousePressEvent = lambda event, img_metadata=self.images[-1]: self.handle_single_click(event, img_metadata)
        label.mouseDoubleClickEvent = lambda event, img_metadata=self.images[-1]: self.handle_double_click(event, img_metadata)
        # Add an "X" button in the top-right corner to allow the user to remove the image
        close_button = QPushButton("✖", image_widget)
        close_button.setStyleSheet(
            """
            QPushButton {
                background-color: rgba(255, 0, 0, 0.7);
                color: white;
                border: none;
                border-radius: 10px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: rgba(255, 0, 0, 1);
            }
            """
        )
        close_button.setFixedSize(20, 20)
        close_button.clicked.connect(lambda: self.remove_image(image_widget))
        close_button.move(100, 0)  # Position the button in the top-right corner
        close_button.hide()  # Initially hidden

        # Add hover events to show/hide the button
        def enter_event(_):
            close_button.show()

        def leave_event(_):
            close_button.hide()

        image_widget.enterEvent = enter_event
        image_widget.leaveEvent = leave_event

        # Add the widgets to the layout
        image_container_layout.addWidget(label)
        image_widget_layout.addWidget(image_container)
        self.image_layout.addWidget(image_widget)

    def remove_image(self, image_widget):
        index_to_remove = None
        for i in range(self.image_layout.count()):
            if self.image_layout.itemAt(i).widget() == image_widget:
                index_to_remove = i
                break
        if index_to_remove is None:
            return

        # Remove the widget from the layout
        self.image_layout.removeWidget(image_widget)
        image_widget.deleteLater()

        # Delete corresponding feature only if it's a temp feature (was drawn but never sent to the MLLM)
        request = QgsFeatureRequest().setFilterExpression(
            f'"ChipId" = \'{self.images[index_to_remove]["chip_id"]}\''
        )
        for feature in self.log_layer.getFeatures(request):
            if str(feature["ImagePath"]) == "NULL":  # Needs to be a temp feature
                self.log_layer.startEditing()
                self.log_layer.dataProvider().deleteFeatures([feature.id()])
                self.log_layer.commitChanges()
                self.log_layer.updateExtents()
                self.log_layer.triggerRepaint()
            break

        # Remove the corresponding image data
        del self.images[index_to_remove]

    def clear_images(self):
        while self.image_layout.count():
            item = self.image_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        self.images.clear()

    def handle_single_click(self, _, img_metadata):
        self.current_image_metadata = img_metadata  # Store metadata for single-click action
        self.single_click_timer.start(200)  # Wait 200ms to differentiate between single and double clicks

    def handle_double_click(self, _, image_metadata):
        self.single_click_timer.stop()  # Cancel the single-click action
        # Execute double-click action
        if image_metadata and image_metadata.get("image_path"):
            image_path = image_metadata["image_path"]
            raw_image_path = image_path.replace("_screen.png", "_raw.png")
            screen_image_exists = os.path.exists(image_path)
            raw_image_exists = os.path.exists(raw_image_path)
            # Determine the available options and prompt the user
            if screen_image_exists and raw_image_exists:
                choice, ok = QInputDialog.getItem(
                    self,
                    "Choose Image to Open",
                    "Select the version of the image to open:",
                    ["Screen", "Raw"],
                    0,
                    False
                )
                if ok:
                    selected_image = image_path if choice == "Screen" else raw_image_path
                    self.open_file(selected_image)  # Open the selected image using the OS default application
            elif screen_image_exists:
                self.open_file(image_path)  # Open the screen image using the OS default application
            elif raw_image_exists:
                self.open_file(raw_image_path)  # Open the raw image using the OS default application
            else:
                QMessageBox.warning(self, "Error", "No available image files to open.")
        else:
            QMessageBox.warning(self, "Error", "Chip has not been sent yet to the MLLM.")

    @staticmethod
    def open_file(image_path):
        if os.name == "nt":  # Windows
            os.startfile(image_path)
        elif os.name == "posix":
            if "darwin" == os.uname().sysname.lower():
                subprocess.run(["open", image_path])  # macOS
            else:
                subprocess.run(["xdg-open", image_path])  # Linux

    def perform_single_click_action(self):
        """Perform the single-click action after timer expires."""
        if self.current_image_metadata:
            self.handle_single_click_action(self.current_image_metadata)

    def handle_single_click_action(self, img_metadata):
        """Actual single-click logic."""
        if img_metadata["chip_id"] is not None:
            request = QgsFeatureRequest().setFilterExpression(f'"ChipId" = \'{img_metadata["chip_id"]}\'')
            for feature in self.log_layer.getFeatures(request):
                zoom_to_and_flash_feature(feature, self.canvas, self.log_layer)
                return


class AreaDrawingTool(QgsMapToolEmitPoint):
    def __init__(self, canvas, on_drawing_finished):
        super().__init__(canvas)
        self.canvas = canvas
        self.on_drawing_finished = on_drawing_finished
        self.rubber_band = QgsRubberBand(self.canvas, QgsWkbTypes.PolygonGeometry)
        self.rubber_band.setColor(Qt.red)  # Set the border color
        self.rubber_band.setWidth(2)  # Set the border width
        self.rubber_band.setFillColor(Qt.transparent)  # Set fill to transparent
        self.start_point = None
        self.is_drawing = False

    def canvasPressEvent(self, event):
        if event.button() == Qt.LeftButton:
            if not self.is_drawing:
                # First click: Start point
                self.start_point = self.toMapCoordinates(event.pos())
                self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
                self.is_drawing = True
            else:
                # Second click: End point
                end_point = self.toMapCoordinates(event.pos())
                self.draw_rectangle(self.start_point, end_point)
                self.on_drawing_finished(QgsRectangle(self.start_point, end_point))
                self.is_drawing = False

    def canvasMoveEvent(self, event):
        if self.is_drawing and self.start_point:
            # Update the rectangle as the cursor moves
            current_point = self.toMapCoordinates(event.pos())
            self.draw_rectangle(self.start_point, current_point)

    def draw_rectangle(self, start_point, end_point):
        rect = QgsRectangle(start_point, end_point)
        points = [
            QgsPointXY(rect.xMinimum(), rect.yMaximum()),  # Top-left
            QgsPointXY(rect.xMaximum(), rect.yMaximum()),  # Top-right
            QgsPointXY(rect.xMaximum(), rect.yMinimum()),  # Bottom-right
            QgsPointXY(rect.xMinimum(), rect.yMinimum()),  # Bottom-left
            QgsPointXY(rect.xMinimum(), rect.yMaximum())   # Close the rectangle
        ]
        self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)  # Clear previous geometry
        for point in points:
            self.rubber_band.addPoint(point, True)
        self.rubber_band.show()


class IdentifyDrawnAreaTool(QgsMapToolEmitPoint):
    def __init__(self, canvas, log_layer, parent_dialog):
        super().__init__(canvas)
        self.canvas = canvas
        self.log_layer = log_layer
        self.parent_dialog = parent_dialog

    def canvasReleaseEvent(self, event):
        clicked_point = self.toMapCoordinates(event.pos())

        # Transform clicked point to EPSG:4326
        crs_src = self.canvas.mapSettings().destinationCrs()
        crs_dest = QgsCoordinateReferenceSystem("EPSG:4326")
        transform = QgsCoordinateTransform(crs_src, crs_dest, QgsProject.instance())
        transformed_point = QgsGeometry.fromPointXY(clicked_point)
        transformed_point.transform(transform)

        layer_features = self.log_layer.getFeatures()

        features, attributes = [], []
        for feature in layer_features:
            if feature.geometry().contains(transformed_point):
                if str(feature.attributes()[1]) != "NULL":
                    features.append(feature)
                    attributes.append(feature.attributes())
                else:  # If it's a temp drawing that hasn't been sent to MLLM yet
                    QMessageBox.information(None, "Feature Info", "This feature has no interactions yet.")
                    return

        if len(attributes) == 0:
            QMessageBox.information(None, "Feature Info", "No feature found at the clicked location.")
            return

        selected_chip, selected_index = self.prompt_selection("chip", attributes, lambda attr: f"Chip ID: {attr[2]}")
        if not selected_chip:
            return

        # Could be optimized if we directly tracked the chips linked to each chat in the database
        all_chats = self.parent_dialog.logs_db.fetch_all_chats()
        all_interactions = {interaction[0]: json.loads(interaction[3]) for interaction in self.parent_dialog.logs_db.fetch_all_interactions()}
        chats = {}
        for chat in all_chats:
            for interaction_id in json.loads(chat[1]):
                if int(selected_chip[2]) in all_interactions[interaction_id]:
                    if chat[2] in chats:
                        chats[chat[2]].append(interaction_id)
                    else:
                        chats[chat[2]] = [interaction_id]

        selected_chat, _ = self.prompt_selection(
            "chat", chats, lambda chat_summary: chat_summary, lambda x: x
        )

        selected_interaction_key, _ = self.prompt_selection(
            "interaction",
            list(selected_chat.values())[0],
            lambda key: f"Interaction ID: {key}"
        )
        if not selected_interaction_key:
            return

        self.open_chat_and_scroll_to_interaction(list(selected_chat.keys())[0], selected_interaction_key)

        feature = features[selected_index]
        zoom_to_and_flash_feature(feature, self.canvas, self.parent_dialog.log_layer)

    @staticmethod
    def prompt_selection(entity_name, options, display_func, return_func=None):
        if len(options) > 1:
            option_strings = [display_func(option) for option in options]
            selected_option, ok = QInputDialog.getItem(
                None,
                f"Select {entity_name.capitalize()}",
                f"Multiple {entity_name}s found. Select one to proceed:",
                option_strings,
                0,
                False
            )
            if not ok:
                return None
            selected_index = option_strings.index(selected_option)
            if entity_name == "chat":
                return {list(options.keys())[selected_index]: list(options.values())[selected_index]}, selected_index
            else:
                return options[selected_index], selected_index
        if return_func:
            return return_func(options), 0
        return options[0], 0

    def open_chat_and_scroll_to_interaction(self, chat_summary, interaction_key):
        for i in range(self.parent_dialog.chat_list.count()):
            item = self.parent_dialog.chat_list.item(i)
            if chat_summary in item.text():
                self.parent_dialog.chat_list.setCurrentItem(item)
                self.parent_dialog.load_chat(item)
                break
        self.scroll_to_interaction(interaction_key)

    def scroll_to_interaction(self, interaction_id):
        # Construct the interaction ID anchor
        interaction_anchor = f"interaction-{interaction_id}"
        # Retrieve the current chat HTML
        chat_html = self.parent_dialog.chat_history.toHtml()
        if interaction_anchor in chat_html:
            highlighted_html = '<p style'.join(
                chat_html.split(f'<a name="{interaction_anchor}">')[0].split('<p style')[:-1] +
                [chat_html.split(f'<a name="{interaction_anchor}">')[0].split('<p style')[-1].replace('="', '=" background-color: yellow;')]
            ) + f'<a name="{interaction_anchor}">' + chat_html.split(f'<a name="{interaction_anchor}">')[1]
            self.parent_dialog.chat_history.setHtml(highlighted_html)
            self.parent_dialog.chat_history.scrollToAnchor(interaction_anchor)
            # Remove highlight after a short duration
            QTimer.singleShot(2000, lambda: self.remove_highlight(interaction_id))
        else:
            QMessageBox.warning(None, "Error", f"Interaction ID {interaction_id} not found.")

    def remove_highlight(self, interaction_id):
        """
        Removes the temporary highlight from the interaction.
        """
        interaction_anchor = f"interaction-{interaction_id}"
        highlighted_html = self.parent_dialog.chat_history.toHtml()
        chat_html = '<p style'.join(
            highlighted_html.split(f'<a name="{interaction_anchor}">')[0].split('<p style')[:-1] +
            [highlighted_html.split(f'<a name="{interaction_anchor}">')[0].split('<p style')[-1].replace(' background-color:#ffff00;','')]
        ) + f'<a name="{interaction_anchor}">' + highlighted_html.split(f'<a name="{interaction_anchor}">')[1]
        self.parent_dialog.chat_history.setHtml(chat_html)
        self.parent_dialog.chat_history.scrollToAnchor(interaction_anchor)
