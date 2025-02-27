import os
import json
import io
import base64
import uuid
from openai import OpenAI
from groq import Groq
import boto3
import tempfile
import ntpath
from PIL import Image
import markdown
import urllib.parse
import ast
import requests

from .settings import SettingsDialog
from .db import LogsDB
from .utils import raw_image_utils as ru
from .custom_qt import (zoom_to_and_flash_feature, CustomTextBrowser, ImageDisplayWidget,
                        AreaDrawingTool, IdentifyDrawnAreaTool)

from qgis.PyQt.QtGui import QPixmap, QImage, QColor, QTextOption, QPalette
from qgis.PyQt.QtCore import QBuffer, QByteArray, Qt, QSettings, QVariant, QSize
from qgis.PyQt.QtWidgets import (QSizePolicy, QFileDialog, QMessageBox, QInputDialog, QComboBox, QLabel, QVBoxLayout,
                                 QPushButton, QWidget, QTextEdit, QApplication, QRadioButton, QHBoxLayout, QDockWidget,
                                 QSplitter, QListWidget, QListWidgetItem)
from qgis.core import (QgsVectorLayer, QgsRasterLayer, QgsSymbol, QgsSimpleLineSymbolLayer, QgsUnitTypes,
                       QgsRectangle, QgsWkbTypes, QgsProject, QgsGeometry, QgsMapRendererParallelJob, QgsFeature,
                       QgsField, QgsVectorFileWriter, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsFeatureRequest, QgsLayerTreeLayer)


class LibreGeoLensDockWidget(QDockWidget):
    def __init__(self, iface, parent=None):
        super(LibreGeoLensDockWidget, self).__init__(parent)
        self.iface = iface
        self.canvas = iface.mapCanvas()

        # ----------------
        # ----------------

        self.current_chat_id = None
        self.conversation = []

        settings = QSettings("Ampsight", "LibreGeoLens")

        self.tracked_layers = []
        self.tracked_layers_names = []
        self.geojson_path = settings.value("geojson_path", None, type=str)
        self.cogs_dict = json.loads(settings.value("cogs_dict", "{}"))
        self.geojson_layer = None
        if self.geojson_path is not None and os.path.exists(self.geojson_path):
            self.handle_imagery_layers()

        self.logs_dir = settings.value("local_logs_directory", "")
        if not self.logs_dir:
            self.logs_dir = os.path.join(os.path.expanduser("~"), "LibreGeoLensLogs")
        os.makedirs(self.logs_dir, exist_ok=True)
        self.logs_db = LogsDB(os.path.join(self.logs_dir, "logs.db"))
        self.logs_db.initialize_database()

        self.current_highlighted_button = None
        self.area_drawing_tool = None
        self.identify_drawn_area_tool = None

        self.log_layer = self.create_log_layer()
        QgsProject.instance().addMapLayer(self.log_layer)
        self.style_geojson_layer(self.log_layer, color=(254, 178, 76))
        # There might be previous temp features (drawings)
        features_to_remove = [
            feature.id() for feature in self.log_layer.getFeatures()
            if str(feature["ImagePath"]) == "NULL"
        ]
        if features_to_remove:
            self.log_layer.startEditing()
            self.log_layer.dataProvider().deleteFeatures(features_to_remove)
            self.log_layer.commitChanges()
        self.log_layer.updateExtents()
        self.log_layer.triggerRepaint()

        # ----------------
        # ----------------

        self.setWindowTitle("LibreGeoLens")
        main_widget = QWidget()
        main_layout = QVBoxLayout(main_widget)

        splitter = QSplitter()
        splitter.setOrientation(Qt.Horizontal)
        splitter.setStretchFactor(0, 1)  # Sidebar
        splitter.setStretchFactor(1, 5)  # Main content
        splitter.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        # ----------------

        sidebar_widget = QWidget()
        sidebar_layout = QVBoxLayout(sidebar_widget)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)

        self.start_new_chat_button = QPushButton("Start New Chat")
        self.start_new_chat_button.clicked.connect(self.start_new_chat)
        sidebar_layout.addWidget(self.start_new_chat_button)

        self.delete_chat_button = QPushButton("Delete Chat")
        self.delete_chat_button.clicked.connect(self.delete_chat)
        sidebar_layout.addWidget(self.delete_chat_button)

        self.chat_list = QListWidget()
        self.chat_list.itemClicked.connect(self.load_chat)
        sidebar_layout.addWidget(self.chat_list)

        buttons_layout = QVBoxLayout()

        button_1_layout = QHBoxLayout()
        buttons_layout.addLayout(button_1_layout)
        self.load_geojson_button = QPushButton("Load GeoJSON")
        self.load_geojson_button.clicked.connect(self.load_geojson)
        self.load_geojson_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        button_1_layout.addWidget(self.load_geojson_button)

        button_2_layout = QHBoxLayout()
        buttons_layout.addLayout(button_2_layout)
        self.get_cogs_button = QPushButton("Draw Area to Stream COGs")
        self.get_cogs_button.clicked.connect(lambda: self.highlight_button(self.get_cogs_button))
        self.get_cogs_button.clicked.connect(lambda: self.activate_area_drawing_tool(capture_image=False))
        self.get_cogs_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        button_2_layout.addWidget(self.get_cogs_button)

        button_3_layout = QHBoxLayout()
        buttons_layout.addLayout(button_3_layout)
        self.draw_area_button = QPushButton("Draw Area to Chip Imagery")
        self.draw_area_button.clicked.connect(lambda: self.highlight_button(self.draw_area_button))
        self.draw_area_button.clicked.connect(lambda: self.activate_area_drawing_tool(capture_image=True))
        self.draw_area_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        button_3_layout.addWidget(self.draw_area_button)

        button_4_layout = QHBoxLayout()
        buttons_layout.addLayout(button_4_layout)
        self.select_area_button = QPushButton("Select Area")
        self.select_area_button.clicked.connect(lambda: self.highlight_button(self.select_area_button))
        self.select_area_button.clicked.connect(self.activate_identify_drawn_area_tool)
        self.select_area_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        button_4_layout.addWidget(self.select_area_button)

        sidebar_layout.addLayout(buttons_layout)

        sidebar_widget.setLayout(sidebar_layout)
        splitter.addWidget(sidebar_widget)

        # ----------------

        main_content_widget = QWidget()
        main_content_layout = QVBoxLayout(main_content_widget)
        main_content_layout.setContentsMargins(0, 0, 0, 0)

        self.chat_history = CustomTextBrowser()
        self.chat_history.setOpenExternalLinks(False)  # Disable automatic link opening
        self.chat_history.anchorClicked.connect(self.handle_anchor_click)  # Connect the click event
        self.chat_history.setReadOnly(True)
        self.chat_history.setWordWrapMode(QTextOption.WordWrap)  # Enable text wrapping
        self.chat_history.setStyleSheet("background-color: #f5f5f5; border: 1px solid #ddd;")
        main_content_layout.addWidget(self.chat_history, stretch=8)

        self.prompt_input = QTextEdit()
        self.prompt_input.setPlaceholderText("Type your prompt here...")
        self.prompt_input.setMinimumHeight(50)
        main_content_layout.addWidget(self.prompt_input, stretch=1)

        self.radio_chip = QRadioButton("Send Screen Chip")
        self.radio_raw = QRadioButton("Send Raw Chip")
        self.radio_chip.setChecked(True)
        radio_group_layout = QHBoxLayout()
        radio_group_layout.addWidget(self.radio_chip)
        radio_group_layout.addWidget(self.radio_raw)
        main_content_layout.addLayout(radio_group_layout)

        self.image_display_widget = ImageDisplayWidget(canvas=self.canvas, log_layer=self.log_layer)
        main_content_layout.addWidget(self.image_display_widget, stretch=2)

        self.send_to_mllm_button = QPushButton("Send to MLLM")
        self.send_to_mllm_button.setStyleSheet("background-color: #4CAF50; color: white; padding: 10px;")
        self.send_to_mllm_button.clicked.connect(self.send_to_mllm_fn)
        main_content_layout.addWidget(self.send_to_mllm_button)

        self.supported_api_clients = {
            "OpenAI": {"class": OpenAI, "models": ["gpt-4o-2024-08-06", "gpt-4o-mini-2024-07-18"]},
            "Groq": {"class": Groq, "models": ["llama-3.2-90b-vision-preview", "llama-3.2-11b-vision-preview"]}
        }
        api_model_layout = QVBoxLayout()

        self.api_label = QLabel("MLLM Service:")
        api_model_layout.addWidget(self.api_label)

        self.api_selection = QComboBox()
        self.api_selection.addItems(list(self.supported_api_clients))
        self.api_selection.currentIndexChanged.connect(self.update_model_choices)
        api_model_layout.addWidget(self.api_selection)

        self.model_label = QLabel("MLLM Model:")
        api_model_layout.addWidget(self.model_label)
        self.model_selection = QComboBox()
        api_model_layout.addWidget(self.model_selection)

        self.update_model_choices()

        main_content_layout.addLayout(api_model_layout, stretch=1)

        main_content_widget.setLayout(main_content_layout)
        splitter.addWidget(main_content_widget)

        # ----------------

        settings = QSettings()
        self.qgis_theme = settings.value("UI/UITheme")
        is_dark_mode = QApplication.palette().color(QPalette.Window).value() < 128
        if self.qgis_theme in ["Night Mapping", "Blend of Gray"] or (self.qgis_theme == "default" and is_dark_mode):
            chat_history_styles = """
            background-color: #2b2b2b;
            color: #ffffff;
            border: 1px solid #555;
            """
            self.text_color = "white"
            image_display_styles = """
            background-color: #2b2b2b;
            """
            self.chat_history.setStyleSheet(chat_history_styles)
            self.image_display_widget.setStyleSheet(image_display_styles)
            if os.name == "posix":
                if "darwin" == os.uname().sysname.lower():  # macOS
                    QApplication.instance().setStyleSheet("""QInputDialog, QComboBox, QPushButton, QLabel {color: #D3D3D3;}""")
                else:  # Linux
                    QApplication.instance().setStyleSheet("""QInputDialog, QComboBox, QPushButton, QLabel {color: #2b2b2b;}""")
        else:
            self.text_color = "black"

        # ----------------

        splitter.setSizes([200, 800])
        main_layout.addWidget(splitter)
        main_widget.setLayout(main_layout)

        self.setWidget(main_widget)

        # ----------------
        # ----------------

        self.load_chat_list()

        self.adjust_size_to_available_space()

        item = self.chat_list.item(self.chat_list.count() - 1)
        if item is None:
            self.start_new_chat()
        else:
            self.chat_list.setCurrentItem(item)
            self.load_chat(item)

    def closeEvent(self, event):

        if self.area_drawing_tool:
            self.area_drawing_tool.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
            self.canvas.unsetMapTool(self.area_drawing_tool)
            self.area_drawing_tool = None

        if self.identify_drawn_area_tool:
            if hasattr(self.identify_drawn_area_tool, "rubber_band"):
                self.identify_drawn_area_tool.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
            self.canvas.unsetMapTool(self.identify_drawn_area_tool)
            self.identify_drawn_area_tool = None

        features_to_remove = [
            feature.id() for feature in self.log_layer.getFeatures()
            if str(feature["ImagePath"]) == "NULL"
        ]
        if features_to_remove:
            self.log_layer.startEditing()
            self.log_layer.dataProvider().deleteFeatures(features_to_remove)
            self.log_layer.commitChanges()
        self.log_layer.updateExtents()
        self.log_layer.triggerRepaint()

        if self.current_highlighted_button:
            self.current_highlighted_button.setStyleSheet("")
            self.current_highlighted_button = None

        self.image_display_widget.clear_images()

        # --- Call the base class implementation to properly close the widget ---
        super(LibreGeoLensDockWidget, self).closeEvent(event)

    def handle_imagery_layers(self):
        """ Handles layers during new init that might have been saved in a QGIS project and thus present before init """

        layers = QgsProject.instance().mapLayersByName("Imagery Polygons")
        if layers:
            self.geojson_layer = layers[0]
            self.tracked_layers.append(self.geojson_layer.id())
            self.tracked_layers_names.append("geojson_layer")
        else:
            self.geojson_layer = QgsVectorLayer(self.geojson_path, "Imagery Polygons", "ogr")
            QgsProject.instance().addMapLayer(self.geojson_layer)
            self.style_geojson_layer(self.geojson_layer)
            self.tracked_layers.append(self.geojson_layer.id())
            self.tracked_layers_names.append("geojson_layer")

        root = QgsProject.instance().layerTreeRoot()
        for node in root.children():
            if isinstance(node, QgsLayerTreeLayer):
                layer = node.layer()
                if layer and layer.id() in self.cogs_dict:
                    self.tracked_layers.append(layer.id())
                    self.tracked_layers_names.append(self.cogs_dict[layer.id()])

    def adjust_size_to_available_space(self):
        """ Adjust the docked widget size to fit within the QGIS interface. """
        # Get available geometry (excluding QGIS toolbars, status bars, etc.)
        available_geometry = QApplication.primaryScreen().availableGeometry()
        dock_width = available_geometry.width() * 0.2
        # Set the size of the dock
        self.setMinimumWidth(int(dock_width))
        self.resize(int(dock_width), int(available_geometry.height()))

    def highlight_button(self, button):
        if self.current_highlighted_button:
            self.current_highlighted_button.setStyleSheet("")
        button.setStyleSheet("font-weight: bold;")
        self.current_highlighted_button = button

    def handle_log_layer(self):
        project = QgsProject.instance()
        project.removeMapLayer(self.log_layer.id())
        del self.log_layer
        self.log_layer = self.create_log_layer()
        QgsProject.instance().addMapLayer(self.log_layer)
        self.style_geojson_layer(self.log_layer, color=(254, 178, 76))
        if self.identify_drawn_area_tool is not None:
            self.identify_drawn_area_tool.log_layer = self.log_layer
        self.image_display_widget.log_layer = self.log_layer

    def handle_anchor_click(self, url):
        url_str = url.toString()
        if url_str.startswith("image://"):
            image_path = urllib.parse.unquote(url_str.replace("image://", "", 1))
            if os.name == "nt":
                image_path = image_path.replace("/", "\\\\").replace("c\\", "C:\\")
            chip_id = ntpath.basename(image_path).split(".")[0].split("_screen")[0]

            # Check if image is already in display widget (use dict comprehension for efficiency)
            existing_images = {img["chip_id"]: idx for idx, img in enumerate(self.image_display_widget.images) 
                               if "chip_id" in img and img["chip_id"] is not None}
            
            if chip_id not in existing_images:
                # Add image to display widget
                self.image_display_widget.add_image(image_path)
                self.image_display_widget.images[-1]["chip_id"] = chip_id
                
                # Get chip geometry data using more efficient query
                chip = self.logs_db.fetch_chip_by_id(chip_id)
                if chip:
                    geocoords = json.loads(chip[2])
                    # Calculate bounds in one pass instead of multiple list comprehensions
                    min_x = min_y = float('inf')
                    max_x = max_y = float('-inf')
                    for lon, lat in geocoords:
                        min_x = min(min_x, lon)
                        max_x = max(max_x, lon)
                        min_y = min(min_y, lat)
                        max_y = max(max_y, lat)
                    
                    rectangle = QgsRectangle(min_x, min_y, max_x, max_y)
                    self.image_display_widget.images[-1]["rectangle_geom"] = QgsGeometry.fromRect(rectangle)

            # Use more efficient feature lookup
            request = QgsFeatureRequest().setFilterExpression(f'"ChipId" = \'{chip_id}\'')
            first_feature = next(self.log_layer.getFeatures(request), None)
            
            if first_feature:
                zoom_to_and_flash_feature(first_feature, self.canvas, self.log_layer)
            else:
                QMessageBox.warning(None, "Feature Not Found", "No feature found for the clicked chip.")

    def load_chat_list(self):
        self.chat_list.clear()
        chats = self.logs_db.fetch_all_chats()
        for chat in chats:
            chat_id, chat_summary = chat[0], chat[2]
            item = QListWidgetItem(chat_summary if chat_summary else f"New chat")
            item.setData(Qt.UserRole, chat_id)
            self.chat_list.addItem(item)

    def start_new_chat(self):
        self.current_chat_id = self.logs_db.save_chat([])
        self.conversation = []
        self.chat_history.clear()
        self.load_chat_list()
        new_chat = self.chat_list.item(self.chat_list.count() - 1)
        self.chat_list.setCurrentItem(new_chat)
        self.load_chat(new_chat)

    def load_chat(self, item):
        chat_id = item.data(Qt.UserRole)
        self.current_chat_id = chat_id
        self.conversation = []
        self.chat_history.clear()

        # Get chat data
        interactions_sequence = json.loads(self.logs_db.fetch_chat_by_id(chat_id)[1])
        
        # Build full HTML content at once rather than appending incrementally
        full_html = []
        
        for interaction_id in interactions_sequence:
            _, prompt, response, chip_ids, mllm_service, mllm_model, chip_modes = self.logs_db.fetch_interaction_by_id(
                interaction_id
            )

            # Add unique interaction ID to the HTML for scrolling
            user_html = (
                f'<div id="interaction-{interaction_id}">'
                f'{markdown.markdown(f"**User:** {prompt}")}'
                f'</div>'
            )
            full_html.append(user_html)

            # Add the interaction to the conversation list
            self.conversation.append({"role": "user", "content": [{"type": "text", "text": prompt}]})

            # Process chips associated with this interaction
            chip_ids_list = json.loads(chip_ids)
            chip_modes_list = ast.literal_eval(chip_modes)
            
            # Optimize image loading - only load visible thumbnails
            for chip_id, chip_mode in zip(chip_ids_list, chip_modes_list):
                image_path = self.logs_db.fetch_chip_by_id(chip_id)[1]
                normalized_path = image_path.replace("\\", "/")
                
                # Use file path for src instead of base64 for thumbnail display
                # This defers actual image loading until display time
                image_html = (
                    f'<div style="position: relative; display: inline-block;">'
                    f'    <a href="image://{normalized_path}" style="text-decoration: none;">'
                    f'        <img src="file:///{normalized_path}" width="75" loading="lazy"/>'
                    f'    </a>'
                    f'    <span style="position: absolute; top: 3px; right: 5px; color: {self.text_color}; font-size: 10px">'
                    f'        ({"Raw" if chip_mode == "raw" else "Screen"} Chip)'
                    f'    </span>'
                    f'</div>'
                )
                full_html.append(image_html)
                
                # For conversation history, we need to load base64 data for API calls
                # But we'll do this only when sending to MLLM, not during chat display
                if chip_mode == "raw":
                    sent_image_path = image_path.replace("_screen.png", "_raw.png")
                else:
                    sent_image_path = image_path
                    
                # Store the image path but don't convert to base64 yet - will do when sending message
                self.conversation[-1]["content"].append(
                    {"type": "local_image_path", "path": sent_image_path, "mode": chip_mode}
                )

            # Add assistant response with unique interaction ID
            assistant_html = (
                f'<div id="interaction-{interaction_id}-response">'
                f'{markdown.markdown(f"**{mllm_model} ({mllm_service}):** {response}")}'
                f'</div>'
            )
            full_html.append(assistant_html)

            # Add the assistant's response to the conversation list
            self.conversation.append({"role": "assistant", "content": response})
            
        # Set the complete HTML content once instead of multiple appends
        self.chat_history.setHtml(''.join(full_html))

    @staticmethod
    def load_image_base64(image_path):
        with open(image_path, 'rb') as f:
            return base64.b64encode(f.read()).decode('utf-8')

    @staticmethod
    def style_geojson_layer(geojson_layer, color=(255, 0, 0)):
        symbol = QgsSymbol.defaultSymbol(geojson_layer.geometryType())
        if symbol is not None:
            line_layer = QgsSimpleLineSymbolLayer()
            line_layer.setColor(QColor(*color))
            line_layer.setWidth(2)
            line_layer.setWidthUnit(QgsUnitTypes.RenderMillimeters)
            symbol.changeSymbolLayer(0, line_layer)
            geojson_layer.renderer().setSymbol(symbol)
        geojson_layer.triggerRepaint()

    def load_geojson(self):
        source, ok = QInputDialog.getItem(
            self.iface.mainWindow(),
            "Select Source",
            "Choose the source of GeoJSON files:",
            ["S3 Directory", "Local Machine", "Use Demo Resources"],
            0,
            False
        )
        if not ok:
            return  # User canceled
        if source == "Local Machine":
            self.load_geojson_from_local()
        elif source == "S3 Directory":
            self.load_geojson_from_s3()
        elif source == "Use Demo Resources":
            self.load_geojson_from_demo()
        settings = QSettings("Ampsight", "LibreGeoLens")
        settings.setValue("geojson_path", self.geojson_path)

    def load_geojson_from_demo(self):
        demo_geojson_path = os.path.join(self.logs_dir, "demo_imagery.geojson")
        if not os.path.exists(demo_geojson_path):
            try:
                response = requests.get("https://libre-geo-lens.s3.us-east-1.amazonaws.com/demo/demo_imagery.geojson")
                response.raise_for_status()
                with open(demo_geojson_path, "wb") as file:
                    file.write(response.content)
            except requests.RequestException as e:
                QMessageBox.critical(self.iface.mainWindow(), "Error", f"Failed to download GeoJSON: {e}")
                return
        self.geojson_path = demo_geojson_path
        self.replace_geojson_layer()

    def load_geojson_from_local(self):
        self.geojson_path, _ = QFileDialog.getOpenFileName(
            self.iface.mainWindow(),
            "Select GeoJSON File",
            "",
            "GeoJSON Files (*.geojson);;All Files (*)"
        )
        if not self.geojson_path:
            return  # User canceled
        self.replace_geojson_layer()

    def load_geojson_from_s3(self):
        settings = QSettings("Ampsight", "LibreGeoLens")
        default_s3_directory = settings.value("default_s3_directory", "")

        s3_path, ok = QInputDialog.getText(
            self.iface.mainWindow(), "S3 Directory Path", "Enter the S3 directory path:", text=default_s3_directory
        )
        if not ok or not s3_path:
            return

        bucket_name, directory_name = s3_path.split("/")[2], '/'.join(s3_path.split("/")[3:])
        s3 = boto3.client('s3')
        response = s3.list_objects_v2(Bucket=bucket_name, Prefix=directory_name)
        if 'Contents' not in response:
            QMessageBox.warning(self.iface.mainWindow(), "Error", "No files found in the specified S3 directory.")
            return

        # Extract GeoJSON files and sort by timestamp (or just sort)
        geojson_files = [
            obj['Key'] for obj in response['Contents']
            if obj['Key'].endswith('.geojson')
        ]
        geojson_files.sort(reverse=True)
        if not geojson_files:
            QMessageBox.warning(self.iface.mainWindow(), "Error", "No GeoJSON files found in the S3 directory.")
            return

        # Prompt user to select a specific file if desired
        file_name, ok = QInputDialog.getItem(
            self.iface.mainWindow(),
            "Select GeoJSON File",
            "Choose a GeoJSON file. It defaults to the latest one.",
            [os.path.basename(f) for f in geojson_files],
            0,
            False
        )
        if ok:
            selected_file = os.path.join(directory_name, file_name)
        else:
            return  # User canceled

        # Download the selected file
        local_path = os.path.join(tempfile.gettempdir(), os.path.basename(selected_file))
        if not os.path.exists(local_path):
            s3.download_file(bucket_name, selected_file, local_path)
        self.geojson_path = local_path
        self.replace_geojson_layer()

    def replace_geojson_layer(self):
        if not self.geojson_path:
            QMessageBox.critical(self.iface.mainWindow(), "Error", "No GeoJSON path set.")
            return

        # Remove previously tracked layers
        project = QgsProject.instance()
        for layer_id in self.tracked_layers:
            layer = project.mapLayer(layer_id)
            if layer:
                project.removeMapLayer(layer_id)
                del layer
        self.tracked_layers.clear()
        self.tracked_layers_names.clear()

        # Load the new GeoJSON layer
        self.geojson_layer = QgsVectorLayer(self.geojson_path, "Imagery Polygons", "ogr")
        if not self.geojson_layer.isValid():
            QMessageBox.critical(self.iface.mainWindow(), "Error", "Failed to load GeoJSON layer.")
            return

        # Add the new GeoJSON layer and style it
        project.addMapLayer(self.geojson_layer)
        self.style_geojson_layer(self.geojson_layer)
        self.tracked_layers.append(self.geojson_layer.id())
        self.tracked_layers_names.append("geojson_layer")

        self.handle_log_layer()

        QMessageBox.information(self.iface.mainWindow(), "Success", "GeoJSON loaded successfully!")

    def create_log_layer(self):
        """
        Load logs.geojson from self.logs_dir if it exists.
        Otherwise, create a new in-memory log layer.
        """
        existing_layer = QgsProject.instance().mapLayersByName("Logs")
        if existing_layer:
            for layer in existing_layer:
                QgsProject.instance().removeMapLayer(layer.id())
                del layer

        logs_path = os.path.join(self.logs_dir, "logs.geojson")
        if os.path.exists(logs_path):
            layer = QgsVectorLayer(logs_path, "Logs", "ogr")
            if not layer.isValid():
                QMessageBox.warning(self, "Error", f"Failed to load {logs_path}. Creating a new log layer.")
                return self._create_memory_log_layer()
            return layer
        else:
            return self._create_memory_log_layer()

    @staticmethod
    def _create_memory_log_layer():
        layer = QgsVectorLayer("Polygon?crs=EPSG:4326", "Logs", "memory")
        provider = layer.dataProvider()
        provider.addAttributes([
            QgsField("Interactions", QVariant.String),
            QgsField("ImagePath", QVariant.String),
            QgsField("ChipId", QVariant.String)
        ])
        layer.updateFields()
        return layer

    def save_logs_to_geojson(self):
        logs_path = os.path.join(self.logs_dir, "logs.geojson")
        QgsVectorFileWriter.writeAsVectorFormat(
            self.log_layer,
            logs_path,
            "utf-8",
            self.log_layer.crs(),
            "GeoJSON"
        )

    @staticmethod
    def save_image_to_buffer(image):
        """Converts the QGIS map image to a buffer in PNG format."""
        # Create a QByteArray to hold the data
        byte_array = QByteArray()
        # Create a QBuffer to wrap around the QByteArray
        buffer = QBuffer(byte_array)
        buffer.open(QBuffer.WriteOnly)
        # Convert the QImage to QPixmap and save it to the buffer
        pixmap = QPixmap.fromImage(image)
        if not pixmap.save(buffer, "PNG"):
            raise ValueError("Failed to save image to buffer")
        buffer.close()
        # Return a Python BytesIO object from the QByteArray data
        return io.BytesIO(byte_array.data())

    def activate_area_drawing_tool(self, capture_image):
        if self.area_drawing_tool:
            self.area_drawing_tool.rubber_band.reset(QgsWkbTypes.PolygonGeometry)  # Clear the previous selection
        if self.identify_drawn_area_tool:
            if hasattr(self.identify_drawn_area_tool, "rubber_band"):
                self.identify_drawn_area_tool.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
            self.identify_drawn_area_tool = None
        self.area_drawing_tool = AreaDrawingTool(
            self.canvas,
            lambda rectangle: self.on_drawing_finished(rectangle, capture_image=capture_image)
        )
        self.canvas.setMapTool(self.area_drawing_tool)

    def on_drawing_finished(self, rectangle, capture_image):
        if not capture_image:
            self.display_cogs_within_rectangle(rectangle)
            return

        rectangle_geom = self.transform_rectangle_crs(rectangle, QgsCoordinateReferenceSystem("EPSG:4326"))

        # Add the drawn area as a temporary feature
        feature = QgsFeature(self.log_layer.fields())
        feature.setGeometry(rectangle_geom)
        chip_id = str(uuid.uuid4())  # temp uuid until the chip is saved if eventually sent to the MLLM
        feature.setAttributes([json.dumps({}), None, chip_id])

        # Capture the image within the drawn area
        image = self.capture_drawn_area(rectangle)
        self.image_display_widget.add_image(image=image)
        self.image_display_widget.images[-1]["rectangle_geom"] = rectangle_geom
        self.image_display_widget.images[-1]["chip_id"] = chip_id

        # Add the feature after capturing the area - otherwise we'll also capture the drawing
        self.log_layer.dataProvider().addFeatures([feature])
        self.log_layer.updateExtents()
        QgsProject.instance().layerTreeRoot().findLayer(self.log_layer.id()).setItemVisibilityChecked(True)
        self.log_layer.triggerRepaint()

    def transform_rectangle_crs(self, rectangle, crs_dest):
        crs_src = self.canvas.mapSettings().destinationCrs()
        transform = QgsCoordinateTransform(crs_src, crs_dest, QgsProject.instance())
        rectangle_geom = QgsGeometry.fromRect(rectangle)
        rectangle_geom.transform(transform)
        return rectangle_geom

    def capture_drawn_area(self, rectangle):
        """Captures the drawn area as an image using the input rectangle"""
        # Set the map settings extent
        settings = self.canvas.mapSettings()
        settings.setExtent(rectangle)

        # Adjust output size to match rectangle aspect ratio
        map_width, map_height = settings.outputSize().width(), settings.outputSize().height()
        aspect_ratio = rectangle.width() / rectangle.height()
        if map_width / map_height > aspect_ratio:
            # Adjust width to match height-based ratio
            new_width = int(map_height * aspect_ratio)
            settings.setOutputSize(QSize(new_width, map_height))
        else:
            # Adjust height to match width-based ratio
            new_height = int(map_width / aspect_ratio)
            settings.setOutputSize(QSize(map_width, new_height))

        image = QImage(settings.outputSize(), QImage.Format_ARGB32_Premultiplied)
        renderer = QgsMapRendererParallelJob(settings)
        renderer.start()
        renderer.waitForFinished()

        return renderer.renderedImage()

    def activate_identify_drawn_area_tool(self):
        if self.area_drawing_tool:
            self.area_drawing_tool.rubber_band.reset(QgsWkbTypes.PolygonGeometry)  # Clear the previous selection
            self.area_drawing_tool = None
        if self.identify_drawn_area_tool and hasattr(self.identify_drawn_area_tool, "rubber_band"):
            self.identify_drawn_area_tool.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
        self.identify_drawn_area_tool = IdentifyDrawnAreaTool(self.canvas, self.log_layer, self)
        self.canvas.setMapTool(self.identify_drawn_area_tool)

    def display_cogs_within_rectangle(self, rectangle):
        """Displays only the COGs within the given rectangle on the QGIS UI
           and ensures logs and polygons layers remain on top."""
        if not self.geojson_path:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Warning",
                f"No GeoJSON has been loaded. Please load a GeoJSON file first."
            )
            return

        rectangle_geom = self.transform_rectangle_crs(rectangle, self.geojson_layer.crs())

        # Record how many layers are currently tracked
        old_count = len(self.tracked_layers)

        # Find features that intersect with the rectangle
        cogs_paths = []
        for feature in self.geojson_layer.getFeatures():
            if feature.geometry().intersects(rectangle_geom):
                remote_path = feature["remote_path"]
                if remote_path and remote_path not in self.tracked_layers_names:
                    cogs_paths.append(remote_path)

        def load_cog(remote_path):
            if remote_path.startswith("s3://"):
                cog_url = f"/vsis3/{remote_path[5:]}"
            elif remote_path.startswith("https://"):
                cog_url = f"/vsicurl/{remote_path}"
            else:
                QMessageBox.warning(
                    self.iface.mainWindow(),
                    "Warning",
                    f"Unsupported remote path format: {remote_path}"
                )
                return
            raster_layer = QgsRasterLayer(cog_url, remote_path.split('/')[-1], "gdal")
            if raster_layer.isValid():
                QgsProject.instance().addMapLayer(raster_layer)
                self.tracked_layers.append(raster_layer.id())
                self.cogs_dict[raster_layer.id()] = remote_path
                settings = QSettings("Ampsight", "LibreGeoLens")
                settings.setValue("cogs_dict", json.dumps(self.cogs_dict))  # Save as JSON string
                self.tracked_layers_names.append(remote_path)
            else:
                QMessageBox.warning(
                    self.iface.mainWindow(),
                    "Warning",
                    f"Failed to load COG: {remote_path}"
                )

        # Load corresponding COGs if it's not too many or give the option to select one to load
        if len(cogs_paths) <= 5:
            for cog_path in cogs_paths:
                load_cog(cog_path)
        else:
            options = [path.split('/')[-1] for path in cogs_paths]
            selected_option, ok = QInputDialog.getItem(
                None,
                "Select Image Outline",
                "Please draw an area that intersects with no more than 5 image outlines or select one of these to load:",
                options,
                0,
                False
            )
            if ok:
                selected_index = options.index(selected_option)
                load_cog(cogs_paths[selected_index])
            else:
                return

        # Reorder layers to ensure log layer and GeoJSON layer remain on top
        root = QgsProject.instance().layerTreeRoot()
        log_layer_node = root.findLayer(self.log_layer.id())
        geojson_layer_node = root.findLayer(self.geojson_layer.id())
        # Move the log layer to the top
        if log_layer_node:
            root.insertChildNode(0, log_layer_node.clone())
            root.removeChildNode(log_layer_node)
        # Move the GeoJSON (polygon) layer to the second position
        if geojson_layer_node:
            root.insertChildNode(1, geojson_layer_node.clone())
            root.removeChildNode(geojson_layer_node)

        # compare old vs. new count of tracked layers
        new_count = len(self.tracked_layers)
        cogs_added = new_count - old_count
        if cogs_added == 0:
            QMessageBox.information(
                self.iface.mainWindow(),
                "No COGs Found",
                "No imagery was found within the drawn rectangle or imagery already loaded."
            )
        else:
            QMessageBox.information(
                self.iface.mainWindow(),
                "Success",
                "COGs within the rectangle have been displayed."
            )

    def update_model_choices(self):
        """Update the model list based on the selected API."""
        api = self.api_selection.currentText()
        models = self.supported_api_clients[api]["models"]
        self.model_selection.clear()
        self.model_selection.addItems(models)

    def delete_chat(self):
        """Delete the selected chat after confirmation"""
        current_item = self.chat_list.currentItem()
        if not current_item:
            QMessageBox.warning(self, "Warning", "Please select a chat to delete.")
            return

        chat_id = current_item.data(Qt.UserRole)
        reply = QMessageBox.question(
            self,
            "Confirm Delete",
            "Are you sure you want to delete this chat? This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )

        if reply == QMessageBox.Yes:
            modified_logs = False
            # Delete chat and get chips to remove from log layer
            for image_path, chip_id in self.logs_db.delete_chat(chat_id):
                # Remove the chip's feature from log layer
                features_to_remove = []
                for feature in self.log_layer.getFeatures():
                    if str(feature["ChipId"]) == str(chip_id):
                        features_to_remove.append(feature.id())
                if features_to_remove:
                    modified_logs = True
                    self.log_layer.startEditing()
                    self.log_layer.dataProvider().deleteFeatures(features_to_remove)
                    self.log_layer.commitChanges()
                # Delete the image files
                if os.path.exists(image_path):
                    os.remove(image_path)
                raw_path = image_path.replace("_screen.png", "_raw.png")
                if os.path.exists(raw_path):
                    os.remove(raw_path)

            # Update UI
            row = self.chat_list.row(current_item)
            self.chat_list.takeItem(row)
            self.chat_history.clear()
            self.image_display_widget.clear_images()
            self.current_chat_id = None
            self.conversation = []

            if modified_logs:
                # If log layer is empty, remove from disk and create from scratch instead of saving changes
                if self.log_layer.featureCount() == 0:
                    os.remove(os.path.join(self.logs_dir, "logs.geojson"))
                    self.handle_log_layer()
                else:
                    # Save changes to geojson
                    self.save_logs_to_geojson()
                    self.handle_log_layer()

            # Start new chat if no chats left
            if self.chat_list.count() == 0:
                self.start_new_chat()

    def save_image_to_logs(self, image, chip_id, raw=False):
        image_dir = os.path.join(self.logs_dir, "chips")
        os.makedirs(image_dir, exist_ok=True)
        # Save the image file in the created directory
        image_path = os.path.join(image_dir, f"{chip_id}_screen.png")
        if raw:
            image.save(image_path.replace("_screen.png", "_raw.png"), "PNG")
        else:
            image.save(image_path, "PNG")
        return image_path

    def send_to_mllm_fn(self):
        try:
            self.send_to_mllm()
        except Exception as e:
            QMessageBox.warning(self.iface.mainWindow(), "Error", str(e))
            self.load_chat(self.chat_list.item(self.chat_list.count() - 1))

    def send_to_mllm(self):
        selected_api = self.api_selection.currentText()
        selected_model = self.model_selection.currentText()
        api_key = os.getenv(selected_api.upper() + "_API_KEY")
        if api_key is None:
            QMessageBox.warning(
                self.iface.mainWindow(), "Error",
                f"{selected_api} API key not set."
                f" Please refer to https://github.com/ampsight/LibreGeoLens?tab=readme-ov-file#mllm-services"
            )
            return
        client = self.supported_api_clients[selected_api]["class"](api_key=api_key)

        prompt = self.prompt_input.toPlainText()
        if not prompt.strip():
            QMessageBox.warning(self, "Error", "Please enter a prompt.")
            return
        
        # First collect user message and prepare data structures
        user_html = f'<div>{markdown.markdown(f"**User:** {prompt}")}</div>'
        self.prompt_input.clear()
        self.conversation.append({"role": "user", "content": [{"type": "text", "text": prompt}]})
        
        # Instead of updating UI for each image, collect HTML for batch update
        image_html_list = []
        
        n_images = len(self.image_display_widget.images)
        chip_ids_sequence, chip_modes_sequence = [], []
        send_raw = self.radio_raw.isChecked()
        
        # Process all images first before updating UI
        for idx in range(n_images):
            image_path = self.image_display_widget.images[idx]["image_path"]
            image_to_send = self.image_display_widget.images[idx]["image"]
            
            # For unsaved images
            if image_path is None:
                rectangle_geom = self.image_display_widget.images[idx]["rectangle_geom"]
                polygon_coords = rectangle_geom.asPolygon()
                chip_id = self.logs_db.save_chip(
                    image_path="tmp_image_path.png",
                    geocoords=[[point.x(), point.y()] for point in polygon_coords[0]] +
                              [[polygon_coords[0][0].x(), polygon_coords[0][0].y()]]
                )
                chip_ids_sequence.append(chip_id)
                image_path = self.save_image_to_logs(image_to_send, chip_id)
                self.image_display_widget.images[idx]["image_path"] = image_path
                self.logs_db.update_chip_image_path(chip_id, image_path)
            else:
                chip_ids_sequence.append(int(ntpath.basename(image_path).split(".")[0].split("_screen")[0]))

            # Process raw chips if needed
            if send_raw:
                chip_modes_sequence.append("raw")
                raw_image_path = image_path.replace("_screen.png", "_raw.png")
                
                if not os.path.exists(raw_image_path):
                    # Raw image doesn't exist yet - need to extract it
                    rectangle = self.image_display_widget.images[idx]["rectangle_geom"].boundingBox()
                    cog_path = ru.find_topmost_cog_feature(rectangle)
                    if cog_path is None:
                        QMessageBox.information(
                            self.iface.mainWindow(),
                            "No Overlapping COG",
                            "No raw imagery layer containing the drawn area could be found."
                        )
                        item = self.chat_list.item(self.chat_list.count() - 1)
                        self.chat_list.setCurrentItem(item)
                        self.load_chat(item)
                        return
                        
                    # Extract and save raw chip
                    drawn_box_geocoords = ru.get_drawn_box_geocoordinates(rectangle, cog_path)
                    chip_width, chip_height = ru.determine_chip_size(drawn_box_geocoords, cog_path)
                    center_latitude = (drawn_box_geocoords.yMinimum() + drawn_box_geocoords.yMaximum()) / 2
                    center_longitude = (drawn_box_geocoords.xMinimum() + drawn_box_geocoords.xMaximum()) / 2
                    
                    image_to_send = ru.extract_chip_from_tif_point_in_memory(
                        img_path=cog_path,
                        center_latitude=center_latitude,
                        center_longitude=center_longitude,
                        chip_width_px=chip_width,
                        chip_height_px=chip_height
                    )
                    self.save_image_to_logs(image_to_send, chip_ids_sequence[-1], raw=True)
                
                self.conversation[-1]["content"].append(
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{self.load_image_base64(raw_image_path)}"}}
                )
            else:
                chip_modes_sequence.append("screen")
                self.conversation[-1]["content"].append(
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{self.load_image_base64(image_path)}"}}
                )
            
            # Create image thumbnail HTML - use file:// URL instead of base64 to reduce HTML size
            normalized_path = image_path.replace("\\", "/")
            image_html = (
                f'<div style="position: relative; display: inline-block;">'
                f'    <a href="image://{normalized_path}" style="text-decoration: none;">'
                f'        <img src="file:///{normalized_path}" width="75" loading="lazy"/>'
                f'    </a>'
                f'    <span style="position: absolute; top: 3px; right: 5px; color: {self.text_color}; font-size: 10px">'
                f'        ({"Raw" if send_raw else "Screen"} Chip)'
                f'    </span>'
                f'</div>'
            )
            image_html_list.append(image_html)
        
        # Update UI with all content at once
        current_html = self.chat_history.toHtml()
        # Append user message and all images
        all_content_html = current_html + user_html + ''.join(image_html_list)
        self.chat_history.setHtml(all_content_html)
        self.chat_history.verticalScrollBar().setValue(self.chat_history.verticalScrollBar().maximum())
        QApplication.processEvents()

        # Stream the response dynamically
        accumulated_text = f"<b>{selected_model} ({selected_api}):</b> "
        full_html = self.chat_history.toHtml()

        # Use an efficient buffer for accumulating large responses
        response_buffer = []

        # Process the conversation to convert any local image paths to base64
        processed_conversation = []
        for message in self.conversation:
            processed_message = {"role": message["role"]}
            
            if message["role"] == "assistant":
                processed_message["content"] = message["content"]
                processed_conversation.append(processed_message)
                continue
                
            processed_content = []
            for content in message["content"]:
                if content.get("type") == "local_image_path":
                    # Convert local image paths to base64
                    image_path = content["path"]
                    if os.path.exists(image_path):
                        processed_content.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{self.load_image_base64(image_path)}"}
                        })
                else:
                    processed_content.append(content)
            
            processed_message["content"] = processed_content
            processed_conversation.append(processed_message)
            
        # Start API call with processed conversation data
        response_stream = client.chat.completions.create(
            model=selected_model,
            messages=processed_conversation,
            stream=True
        )

        # Process the stream with fewer UI updates
        update_counter = 0
        update_frequency = 2  # Update UI every N chunks to reduce UI redraws

        for chunk in response_stream:
            content = chunk.choices[0].delta.content
            if content is None:
                continue

            response_buffer.append(content)
            update_counter += 1

            # Only update UI periodically to improve performance
            if update_counter >= update_frequency:
                accumulated_text += ''.join(response_buffer)
                rendered_markdown = markdown.markdown(accumulated_text)
                updated_html = full_html + rendered_markdown
                self.chat_history.setHtml(updated_html)
                self.chat_history.verticalScrollBar().setValue(self.chat_history.verticalScrollBar().maximum())
                QApplication.processEvents()
                response_buffer = []
                update_counter = 0

        # Final update with any remaining content
        if response_buffer:
            accumulated_text += ''.join(response_buffer)

        # Complete response
        response = accumulated_text.replace(f"<b>{selected_model} ({selected_api}):</b> ", "")
        full_html += markdown.markdown(accumulated_text)
        self.chat_history.setHtml(full_html)

        self.conversation.append({"role": "assistant", "content": response})
        interaction_id = self.logs_db.save_interaction(
            text_input=prompt, text_output=response,
            chips_sequence=chip_ids_sequence,
            mllm_service=selected_api, mllm_model=selected_model,
            chips_mode_sequence=chip_modes_sequence,
        )
        self.logs_db.add_new_interaction_to_chat(self.current_chat_id, interaction_id)

        summary = client.chat.completions.create(
            model=selected_model,
            messages=[
                {"role": "user", "content": [{"type": "text", "text":
                    f"Summarize the following in 10 words or less: {self.chat_history.toPlainText()}."
                    f" Only respond with your summary."}]}]
        ).choices[0].message.content.strip()
        self.logs_db.update_chat_summary(self.current_chat_id, summary)
        self.chat_list.currentItem().setText(summary)

        for idx in range(n_images):
            request = QgsFeatureRequest().setFilterExpression(
                f'"ChipId" = \'{self.image_display_widget.images[idx]["chip_id"]}\''
            )
            for feature in self.log_layer.getFeatures(request):
                feat_attrs = feature.attributes()
                interactions = feat_attrs[0]
                if type(interactions) == str:
                    interactions = json.loads(interactions)
                if len(interactions) > 0:
                    interactions[interaction_id] = {"prompt": prompt, "response": response}
                    self.log_layer.dataProvider().changeAttributeValues({
                        feature.id(): {0: json.dumps(interactions)}
                    })
                else:
                    interactions[interaction_id] = {"prompt": prompt, "response": response}
                    self.image_display_widget.images[idx]["chip_id"] = chip_ids_sequence[idx]
                    self.log_layer.dataProvider().changeAttributeValues({
                        feature.id(): {0: json.dumps(interactions),
                                       1: self.image_display_widget.images[idx]["image_path"],
                                       2: self.image_display_widget.images[idx]["chip_id"]}
                    })
                break
        if n_images > 0:
            self.log_layer.updateExtents()
            self.save_logs_to_geojson()
            self.handle_log_layer()
            if self.area_drawing_tool:
                self.area_drawing_tool.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
            self.image_display_widget.clear_images()

        settings_dialog = SettingsDialog(self.iface.mainWindow())
        settings_dialog.sync_local_logs_dir_with_s3(self.logs_dir)

        # Reload chat to offload in-memory imagery in self.conversation
        item = self.chat_list.currentItem()
        self.chat_list.setCurrentItem(item)
        self.load_chat(item)
        self.chat_history.verticalScrollBar().setValue(self.chat_history.verticalScrollBar().maximum())
