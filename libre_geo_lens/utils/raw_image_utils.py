import rasterio
import numpy as np
from rasterio.windows import from_bounds
from PIL import Image
from qgis.core import (
    QgsRectangle, QgsGeometry, QgsProject, QgsLayerTreeLayer,
    QgsCoordinateTransform, QgsCoordinateReferenceSystem
)
from qgis.PyQt.QtGui import QImage
from pyproj import Transformer


def find_topmost_cog_feature(drawn_rectangle):
    """
    Looks from top of the QGIS layer tree downward, finding the first layer
    whose extent intersects the drawn rectangle in `canvas`.
    Returns the associated path.
    """
    drawn_geom = QgsGeometry.fromRect(drawn_rectangle)
    source_crs = QgsCoordinateReferenceSystem("EPSG:4326")
    root = QgsProject.instance().layerTreeRoot()

    for node in root.children():
        if not isinstance(node, QgsLayerTreeLayer):
            continue

        layer = node.layer()
        if ".geojson" in layer.source() or "Polygon?crs=EPSG:4326&uid=" in layer.source():
            continue

        layer_crs = layer.crs()

        # Transform the drawn rectangle from the source CRS to the layer's CRS
        transform = QgsCoordinateTransform(source_crs, layer_crs, QgsProject.instance())
        drawn_geom_layer_crs = QgsGeometry(drawn_geom)
        drawn_geom_layer_crs.transform(transform)

        # Get the layer's extent as a QgsGeometry
        layer_extent_geom = QgsGeometry.fromRect(layer.extent())

        # Check for intersection
        if layer_extent_geom.intersects(drawn_geom_layer_crs):
            return layer.source()

    return None

def get_drawn_box_geocoordinates(drawn_rectangle, image_path):
    """
    1. Reads the CRS of the GeoTIFF from `image_path`.
    2. Transforms `drawn_rectangle` (in the canvas CRS) into the TIFF's CRS.
    3. Extracts the bounding box in the TIFF's CRS.
    4. Transforms that bounding box to EPSG:4326 and returns it as a QgsRectangle.
    """
    # (A) Read the TIFF’s CRS with rasterio
    with rasterio.open(image_path) as ds:
        tiff_crs = ds.crs

    # (B) Transform the drawn rectangle from source CRS to TIFF CRS
    source_crs = QgsCoordinateReferenceSystem("EPSG:4326")
    to_tiff_transform = Transformer.from_crs(source_crs.authid(), tiff_crs.to_string(), always_xy=True)

    # Extract the raw bounds (in source CRS)
    x_min_source = drawn_rectangle.xMinimum()
    y_min_source = drawn_rectangle.yMinimum()
    x_max_source = drawn_rectangle.xMaximum()
    y_max_source = drawn_rectangle.yMaximum()

    # Transform the four corners to TIFF CRS
    min_x_tiff, min_y_tiff = to_tiff_transform.transform(x_min_source, y_min_source)
    max_x_tiff, max_y_tiff = to_tiff_transform.transform(x_max_source, y_max_source)

    # At this point, we have the bounding box in the TIFF's CRS
    # (C) Transform that bounding box to EPSG:4326, if TIFF CRS is not already EPSG:4326
    if tiff_crs.to_string() != "EPSG:4326":
        to_epsg4326 = Transformer.from_crs(tiff_crs.to_string(), "EPSG:4326", always_xy=True)
        min_lon, min_lat = to_epsg4326.transform(min_x_tiff, min_y_tiff)
        max_lon, max_lat = to_epsg4326.transform(max_x_tiff, max_y_tiff)
    else:
        # TIFF is already EPSG:4326
        min_lon, min_lat = min_x_tiff, min_y_tiff
        max_lon, max_lat = max_x_tiff, max_y_tiff

    # Build and return a QgsRectangle in EPSG:4326 coords
    geocoords_box = QgsRectangle(min_lon, min_lat, max_lon, max_lat)

    return geocoords_box

def determine_chip_size(geocoords, img_path):
    """
    Determine the chip size based on the geocoordinate dimensions
    and raster resolution.
    Returns width and height in pixels.
    """
    # Extract the bounds from the geocoordinates
    left = geocoords.xMinimum()
    right = geocoords.xMaximum()
    bottom = geocoords.yMinimum()
    top = geocoords.yMaximum()

    # Open the raster file to get resolution
    with rasterio.open(img_path) as src:
        raster_crs = src.crs
        raster_resolution_x = abs(src.transform.a)  # Units per pixel in X
        raster_resolution_y = abs(src.transform.e)  # Units per pixel in Y

        # Prevent division by zero
        if raster_resolution_x == 0 or raster_resolution_y == 0:
            raise ValueError("Raster resolution cannot be zero.")

        # Transform geocoordinates to raster CRS if necessary
        if raster_crs.to_string() != "EPSG:4326":
            transformer = Transformer.from_crs("EPSG:4326", raster_crs, always_xy=True)
            left, bottom = transformer.transform(left, bottom)
            right, top = transformer.transform(right, top)

        # Calculate the chip dimensions in units
        width_in_units = right - left
        height_in_units = top - bottom

        if width_in_units <= 0 or height_in_units <= 0:
            raise ValueError("Invalid bounding box (non-positive width/height).")

        # Convert to pixels based on the raster resolution
        chip_width_in_pixels = int(width_in_units / raster_resolution_x)
        chip_height_in_pixels = int(height_in_units / raster_resolution_y)

        # Ensure chip sizes are at least 1 pixel
        chip_width_in_pixels = max(1, chip_width_in_pixels)
        chip_height_in_pixels = max(1, chip_height_in_pixels)

    return chip_width_in_pixels, chip_height_in_pixels


def extract_chip_from_tif_point_in_memory(img_path, center_latitude, center_longitude, chip_width_px, chip_height_px):
    """
    Extract a square chip from a GeoTIFF using Rasterio, centered on
    (center_longitude, center_latitude). Return the PNG image bytes
    (in memory) instead of writing to disk.
    """
    with rasterio.open(img_path) as src:
        # If necessary, transform (lon/lat) from EPSG:4326 -> the raster's CRS
        if src.crs.to_string() != "EPSG:4326":
            transformer = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
            center_longitude, center_latitude = transformer.transform(
                center_longitude, center_latitude
            )

        # Calculate window based on raster resolution
        x_res = abs(src.transform.a)
        y_res = abs(src.transform.a)
        half_width_units = (chip_width_px / 2) * x_res
        half_height_units = (chip_height_px / 2) * y_res

        min_x = center_longitude - half_width_units
        max_x = center_longitude + half_width_units
        min_y = center_latitude - half_height_units
        max_y = center_latitude + half_height_units

        window = from_bounds(min_x, min_y, max_x, max_y, transform=src.transform)
        data = src.read(window=window)

        # Validate we actually got data
        if data.shape[1] == 0 or data.shape[2] == 0:
            raise ValueError(
                "The requested chip window is empty or invalid (out-of-bounds). "
                f"Shape={data.shape}"
            )

        # Determine whether RGB, RGBA, or single-band
        bands, height, width = data.shape
        if bands == 3:
            mode = "RGB"
        elif bands == 4:
            mode = "RGBA"
        else:
            mode = "L"  # single-band (e.g., grayscale)

        # If data isn't uint8, normalize to [0..255]
        if data.dtype != np.uint8:
            data_min = np.min(data)
            data_max = np.max(data)
            if data_max - data_min == 0:
                # Avoid divide-by-zero if raster is constant
                data_normalized = np.zeros_like(data, dtype=np.uint8)
            else:
                data_normalized = ((data - data_min) / (data_max - data_min) * 255).astype(np.uint8)
        else:
            data_normalized = data

        # PIL expects (height, width, channels)
        if bands > 1:
            data_for_pil = data_normalized.transpose(1, 2, 0)
        else:
            data_for_pil = data_normalized[0]

        # Build a PIL image
        pil_img = Image.fromarray(data_for_pil, mode=mode)
        if pil_img.mode != "RGBA":
            pil_img = pil_img.convert("RGBA")
        data = pil_img.tobytes("raw", "RGBA")
        image_to_send = QImage(data, pil_img.width, pil_img.height, QImage.Format_RGBA8888)

        return image_to_send
