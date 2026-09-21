import json
from importlib.resources import files
from benchweave_sdk.validation import validate_descriptor


def test_descriptor_valid_and_accurate():
    descriptor = json.loads(files("benchweave_emeet_c960").joinpath("descriptor.json").read_text())
    validate_descriptor(descriptor)
    assert descriptor["otdp_version"] == "0.3.0"
    assert descriptor["id"] == "dev.emeet.c960"
    assert descriptor["capabilities"] == ["identify", "read", "write"]
    assert "otdp.core/0.3.0" in descriptor["required_features"]
    names = [p["name"] for p in descriptor["parameters"]]
    assert len(names) == 16
    assert names == [
        "brightness", "contrast", "saturation", "hue", "white_balance_automatic",
        "white_balance_temperature", "gamma", "gain", "power_line_frequency",
        "sharpness", "backlight_compensation", "auto_exposure",
        "exposure_time_absolute", "focus_automatic_continuous", "focus_absolute",
        "zoom_absolute",
    ]
    by_name = {p["name"]: p for p in descriptor["parameters"]}
    assert by_name["brightness"]["range"] == [-64, 64]
    assert by_name["power_line_frequency"]["enum_values"] == ["Disabled", "50 Hz", "60 Hz"]
    assert by_name["auto_exposure"]["enum_values"] == ["Manual", "Aperture-Priority"]
    assert by_name["white_balance_automatic"]["type"] == "bool"
    assert all(p["access"] == "rw" for p in descriptor["parameters"])
    assert all("hazard_class" in p for p in descriptor["parameters"])


def test_provenance_references_resolve_in_package():
    descriptor = json.loads(files("benchweave_emeet_c960").joinpath("descriptor.json").read_text())
    package = files("benchweave_emeet_c960")
    references = [s["reference"] for s in descriptor["provenance"]["sources"]]
    references += [v["path"] for v in descriptor["provenance"]["test_vectors"]]
    for reference in references:
        assert package.joinpath(reference).is_file(), reference
