import rasterio
from rasterio.warp import transform
import os
import json
import boto3
import argparse
from datetime import datetime
from tqdm import tqdm
import logging
logging.getLogger("botocore").setLevel(logging.WARNING)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def parse_s3_path(s3_path):
    """ Parse S3 path into bucket and prefix. """
    if not s3_path.startswith("s3://"):
        raise ValueError("S3 path must start with 's3://'")
    parts = s3_path[5:].split("/", 1)
    bucket = parts[0]
    prefix = parts[1] if len(parts) > 1 else ""
    return bucket, prefix


def list_files_in_s3_directory(s3_directory, file_extensions=None):
    """ Recursively lists all files in the specified S3 directory and filters by extensions if provided. """
    s3 = boto3.client("s3")
    bucket, prefix = parse_s3_path(s3_directory)
    paginator = s3.get_paginator("list_objects_v2")
    page_iterator = paginator.paginate(Bucket=bucket, Prefix=prefix)

    files = []
    for page in page_iterator:
        if "Contents" in page:
            for obj in page["Contents"]:
                key = obj["Key"]
                if not key.endswith("/") and (not file_extensions or any(key.endswith(ext) for ext in file_extensions)):
                    full_path = f"s3://{bucket}/{key}"
                    files.append(full_path)
    return files


def extract_geocoordinates_rasterio(file_path, target_crs="EPSG:4326"):
    try:
        with rasterio.open(file_path) as src:
            # Extract the corner coordinates
            bounds = src.bounds
            # Check the CRS of the GeoTIFF
            crs = src.crs
            if crs is None:
                logger.error(f"The GeoTIFF {file_path} does not have a defined CRS. Skipping this file.")
                return None

            # Get the coordinates of the four corners
            corners = [
                (bounds.left, bounds.top),  # Top-left
                (bounds.right, bounds.top),  # Top-right
                (bounds.right, bounds.bottom),  # Bottom-right
                (bounds.left, bounds.bottom),  # Bottom-left
                (bounds.left, bounds.top)  # Close the polygon
            ]

            # Reproject coordinates to the target CRS
            x_coords, y_coords = zip(*corners)
            x_reproj, y_reproj = transform(crs, target_crs, x_coords, y_coords)
            corners_reproj = list(zip(x_reproj, y_reproj))

        return corners_reproj
    except Exception as e:
        logger.error(f"An error occurred while processing {file_path}: {e}")
        return None


def geojson_conversion(image_paths):
    geojson = {
        "type": "FeatureCollection",
        "features": [],
    }

    with tqdm(total=len(image_paths), desc="Processing S3 paths", unit="file") as pbar:
        for image_path in image_paths:
            if image_path.endswith(".tif"):
                polygon = extract_geocoordinates_rasterio(image_path)
            else:
                logger.error(f"Unsupported file type: {image_path}")
                pbar.update(1)
                continue

            if not polygon:
                logger.error(f"Could not extract geocoordinates for {image_path}")
                pbar.update(1)
                continue

            feature = {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [polygon],
                },
                "properties": {
                    "remote_path": image_path
                },
            }
            geojson["features"].append(feature)
            pbar.update(1)

    return geojson


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Creates .geojson with COG imagery outlines and remote paths from data in S3 to use with LibreGeoLens."
    )

    parser.add_argument(
        "--s3_directories",
        nargs='+',
        required=True,
        help="List of S3 directories to process. Example: s3://bucket1/path/to/dir1/ s3://bucket2/path/to/dir2/"
    )
    parser.add_argument(
        "--out_dir",
        type=str,
        required=False,
        default=".",
        help="Output directory where the .geojson will be saved to."
    )

    args = parser.parse_args()

    s3_paths = []
    for directory in args.s3_directories:
        logger.info(f"Processing directory: {directory}")
        files = list_files_in_s3_directory(directory, [".tif"])
        s3_paths.extend(files)

    geojson_data = geojson_conversion(s3_paths)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    unique_filename = f"imagery_{timestamp}.geojson"

    os.makedirs(args.out_dir, exist_ok=True)
    out_path = os.path.join(args.out_dir, unique_filename)
    logger.info(f"Saving GeoJSON file to {out_path}")
    with open(out_path, "w") as f:
        json.dump(geojson_data, f, indent=4)
