def classFactory(iface):  # pylint: disable=invalid-name
    from .libre_geo_lens import LibreGeoLens
    return LibreGeoLens(iface)
