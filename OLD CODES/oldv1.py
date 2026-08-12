import os
from collections import Counter
from copy import deepcopy
import xml.etree.ElementTree as ET
from xml.dom import minidom

import streamlit as st
from PIL import Image, ImageDraw
from streamlit_drawable_canvas import st_canvas


# ------------------ CONFIG ------------------
IMAGE_DIR = "images"
ANNOTATION_DIR = "annotations"
MIN_BOX_SIZE = 5

LABELS = ["RBC", "WBC", "Platelets"]

LABEL_COLORS = {
    "RBC": "#D0021B",
    "WBC": "#4A90E2",
    "Platelets": "#7ED321",
}

os.makedirs(ANNOTATION_DIR, exist_ok=True)


# ------------------ SESSION STATE ------------------
def initialize_session_state() -> None:
    defaults = {
        "selected_type": LABELS[0],
        "annotations": [],
        "annotation_counts": {label: 0 for label in LABELS},
        "annotation_store": {},
        "canvas_object_count": 0,
        "current_image_index": 0,
        "canvas_revision": 0,
        "active_image_name": None,
    }

    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = deepcopy(value)


# ------------------ IMAGE HELPERS ------------------
def get_image_files() -> list[str]:
    if not os.path.isdir(IMAGE_DIR):
        return []

    return sorted(
        [
            file_name
            for file_name in os.listdir(IMAGE_DIR)
            if file_name.lower().endswith((".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"))
        ]
    )


def get_unannotated_images() -> list[str]:
    images = get_image_files()
    unannotated = []
    for image_name in images:
        xml_name = os.path.splitext(image_name)[0] + ".xml"
        if not os.path.exists(os.path.join(ANNOTATION_DIR, xml_name)):
            unannotated.append(image_name)
    return unannotated


def get_current_image_name(images: list[str]) -> str | None:
    if not images:
        return None

    st.session_state.current_image_index = max(0, min(st.session_state.current_image_index, len(images) - 1))
    return images[st.session_state.current_image_index]


def load_image(image_name: str) -> Image.Image:
    return Image.open(os.path.join(IMAGE_DIR, image_name)).convert("RGB")


# ------------------ ANNOTATION STATE ------------------
def current_image_key(image_name: str) -> str:
    return image_name


def load_annotations_for_image(image_name: str) -> None:
    key = current_image_key(image_name)
    st.session_state.annotations = deepcopy(st.session_state.annotation_store.get(key, []))
    st.session_state.canvas_object_count = 0
    refresh_annotation_counts()


def activate_image(image_name: str) -> None:
    previous_image = st.session_state.active_image_name
    if previous_image and previous_image != image_name:
        persist_annotations_for_image(previous_image)

    if previous_image != image_name:
        load_annotations_for_image(image_name)
        st.session_state.active_image_name = image_name
        st.session_state.canvas_revision += 1


def persist_annotations_for_image(image_name: str) -> None:
    st.session_state.annotation_store[current_image_key(image_name)] = deepcopy(st.session_state.annotations)


def sync_canvas_count_with_existing_objects(canvas_objects: list[dict]) -> None:
    st.session_state.canvas_object_count = min(st.session_state.canvas_object_count, len(canvas_objects))


def undo_last_box() -> None:
    if st.session_state.annotations:
        st.session_state.annotations.pop()
        refresh_annotation_counts()
        st.session_state.canvas_revision += 1


def clear_all_boxes() -> None:
    st.session_state.annotations = []
    st.session_state.canvas_object_count = 0
    refresh_annotation_counts()
    st.session_state.canvas_revision += 1


def refresh_annotation_counts() -> None:
    counts = Counter(annotation["label"] for annotation in st.session_state.annotations)
    st.session_state.annotation_counts = {label: int(counts.get(label, 0)) for label in LABELS}


# ------------------ GEOMETRY HELPERS ------------------
def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def extract_canvas_box(obj: dict, canvas_width: float, canvas_height: float, image_width: int, image_height: int) -> dict | None:
    left = float(obj.get("left", 0))
    top = float(obj.get("top", 0))
    width = abs(float(obj.get("width", 0))) * abs(float(obj.get("scaleX", 1)))
    height = abs(float(obj.get("height", 0))) * abs(float(obj.get("scaleY", 1)))

    if width <= 0 or height <= 0:
        return None

    xmin = left
    ymin = top
    xmax = left + width
    ymax = top + height

    scale_x = image_width / canvas_width if canvas_width else 1.0
    scale_y = image_height / canvas_height if canvas_height else 1.0

    xmin = xmin * scale_x
    xmax = xmax * scale_x
    ymin = ymin * scale_y
    ymax = ymax * scale_y

    xmin, xmax = sorted((xmin, xmax))
    ymin, ymax = sorted((ymin, ymax))

    xmin = clamp(xmin, 0, image_width)
    ymin = clamp(ymin, 0, image_height)
    xmax = clamp(xmax, 0, image_width)
    ymax = clamp(ymax, 0, image_height)

    xmin_i = int(round(xmin))
    ymin_i = int(round(ymin))
    xmax_i = int(round(xmax))
    ymax_i = int(round(ymax))

    if xmax_i - xmin_i < MIN_BOX_SIZE or ymax_i - ymin_i < MIN_BOX_SIZE:
        return None

    if xmin_i >= xmax_i or ymin_i >= ymax_i:
        return None

    return {
        "xmin": xmin_i,
        "ymin": ymin_i,
        "xmax": xmax_i,
        "ymax": ymax_i,
    }


def validate_annotation(annotation: dict, image_width: int, image_height: int) -> bool:
    xmin = int(annotation["xmin"])
    ymin = int(annotation["ymin"])
    xmax = int(annotation["xmax"])
    ymax = int(annotation["ymax"])

    if not (0 <= xmin < xmax <= image_width):
        return False
    if not (0 <= ymin < ymax <= image_height):
        return False
    if (xmax - xmin) < MIN_BOX_SIZE or (ymax - ymin) < MIN_BOX_SIZE:
        return False
    return True


def normalize_annotation(annotation: dict, image_width: int, image_height: int) -> dict | None:
    xmin = int(round(clamp(float(annotation["xmin"]), 0, image_width)))
    ymin = int(round(clamp(float(annotation["ymin"]), 0, image_height)))
    xmax = int(round(clamp(float(annotation["xmax"]), 0, image_width)))
    ymax = int(round(clamp(float(annotation["ymax"]), 0, image_height)))

    xmin, xmax = sorted((xmin, xmax))
    ymin, ymax = sorted((ymin, ymax))

    clipped = {
        "label": annotation["label"],
        "xmin": xmin,
        "ymin": ymin,
        "xmax": xmax,
        "ymax": ymax,
    }

    return clipped if validate_annotation(clipped, image_width, image_height) else None


def add_annotation(annotation: dict, image_width: int, image_height: int) -> bool:
    normalized = normalize_annotation(annotation, image_width, image_height)
    if normalized is None:
        return False

    st.session_state.annotations.append(normalized)
    refresh_annotation_counts()
    return True


def ingest_new_canvas_objects(canvas_objects: list[dict], selected_type: str, canvas_width: int, canvas_height: int, image_width: int, image_height: int) -> None:
    sync_canvas_count_with_existing_objects(canvas_objects)
    if st.session_state.canvas_object_count >= len(canvas_objects):
        return

    new_objects = canvas_objects[st.session_state.canvas_object_count :]
    for obj in new_objects:
        parsed_box = extract_canvas_box(obj, canvas_width, canvas_height, image_width, image_height)
        if parsed_box is None:
            continue

        annotation = {
            "label": selected_type,
            **parsed_box,
        }
        add_annotation(annotation, image_width, image_height)

    st.session_state.canvas_object_count = len(canvas_objects)
    persist_annotations_for_image(st.session_state.active_image_name)


# ------------------ XML HELPERS ------------------
def build_pascal_voc_xml(image_name: str, image_width: int, image_height: int, annotations: list[dict]) -> ET.Element:
    root = ET.Element("annotation")
    ET.SubElement(root, "folder").text = os.path.basename(IMAGE_DIR)
    ET.SubElement(root, "filename").text = image_name
    ET.SubElement(root, "segmented").text = "0"

    size = ET.SubElement(root, "size")
    ET.SubElement(size, "width").text = str(image_width)
    ET.SubElement(size, "height").text = str(image_height)
    ET.SubElement(size, "depth").text = "3"

    for annotation in annotations:
        object_node = ET.SubElement(root, "object")
        ET.SubElement(object_node, "name").text = annotation["label"]
        ET.SubElement(object_node, "pose").text = "Unspecified"
        ET.SubElement(object_node, "truncated").text = "0"
        ET.SubElement(object_node, "difficult").text = "0"

        bndbox = ET.SubElement(object_node, "bndbox")
        ET.SubElement(bndbox, "xmin").text = str(int(annotation["xmin"]))
        ET.SubElement(bndbox, "ymin").text = str(int(annotation["ymin"]))
        ET.SubElement(bndbox, "xmax").text = str(int(annotation["xmax"]))
        ET.SubElement(bndbox, "ymax").text = str(int(annotation["ymax"]))

    return root


def prettify_xml(element: ET.Element) -> str:
    rough_xml = ET.tostring(element, encoding="utf-8")
    pretty = minidom.parseString(rough_xml).toprettyxml(indent="    ")
    lines = [line for line in pretty.splitlines() if line.strip()]
    return "\n".join(lines) + "\n"


def save_pascal_voc(image_name: str, image_width: int, image_height: int, annotations: list[dict]) -> tuple[bool, str]:
    valid_annotations = []
    for annotation in annotations:
        normalized = normalize_annotation(annotation, image_width, image_height)
        if normalized is None:
            return False, "One or more bounding boxes are invalid and could not be saved."
        valid_annotations.append(normalized)

    if not valid_annotations:
        return False, "Please annotate at least one valid bounding box before saving."

    root = build_pascal_voc_xml(image_name, image_width, image_height, valid_annotations)
    xml_path = os.path.join(ANNOTATION_DIR, os.path.splitext(image_name)[0] + ".xml")

    with open(xml_path, "w", encoding="utf-8") as file_handle:
        file_handle.write(prettify_xml(root))

    return True, f"Annotation saved for {image_name}"


# ------------------ VISUAL HELPERS ------------------
def hex_to_rgba(color_hex: str, alpha: int = 100) -> tuple[int, int, int, int]:
    color_rgb = tuple(int(color_hex.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4))
    return color_rgb + (alpha,)


def draw_colored_boxes(image: Image.Image, boxes: list[dict]) -> Image.Image:
    preview = image.copy()
    draw = ImageDraw.Draw(preview, "RGBA")

    for box in boxes:
        color_hex = LABEL_COLORS.get(box["label"], "#FFFFFF")
        draw.rectangle(
            [(int(box["xmin"]), int(box["ymin"])), (int(box["xmax"]), int(box["ymax"]))],
            fill=hex_to_rgba(color_hex, 80),
            outline=color_hex,
            width=3,
        )

    return preview


def annotation_counts(annotations: list[dict]) -> Counter:
    return Counter(annotation["label"] for annotation in annotations)


# ------------------ STREAMLIT UI ------------------
st.set_page_config(layout="wide")
st.title("Edge-AI Based Automated Hematology Screening System Using Digital Microscopy")

initialize_session_state()

images = get_unannotated_images()
if not images:
    st.success("All images annotated!")
    st.stop()

current_image_name = get_current_image_name(images)
if current_image_name is None:
    st.success("All images annotated!")
    st.stop()

image = load_image(current_image_name)
width, height = image.size

activate_image(current_image_name)

st.subheader(f"Annotating: {current_image_name}")

nav_col1, nav_col2, nav_col3 = st.columns([1, 1, 4])
with nav_col1:
    if st.button("Previous Image", disabled=st.session_state.current_image_index <= 0):
        persist_annotations_for_image(current_image_name)
        st.session_state.current_image_index -= 1
        st.rerun()
with nav_col2:
    if st.button("Next Image", disabled=st.session_state.current_image_index >= len(images) - 1):
        persist_annotations_for_image(current_image_name)
        st.session_state.current_image_index += 1
        st.rerun()

st.markdown("**Color Legend:**")
col_legend1, col_legend2, col_legend3 = st.columns(3)
with col_legend1:
    st.markdown("Red = **RBC**")
with col_legend2:
    st.markdown("Blue = **WBC**")
with col_legend3:
    st.markdown("Green = **Platelets**")

st.markdown("---")
st.markdown("### Cell Counts")
pred_col1, pred_col2, pred_col3 = st.columns(3)
counts = annotation_counts(st.session_state.annotations)
with pred_col1:
    st.metric("RBC Count", st.session_state.annotation_counts.get("RBC", 0))
with pred_col2:
    st.metric("WBC Count", st.session_state.annotation_counts.get("WBC", 0))
with pred_col3:
    st.metric("Platelets Count", st.session_state.annotation_counts.get("Platelets", 0))

st.markdown("---")

preview_image = draw_colored_boxes(image, st.session_state.annotations)
st.image(preview_image, caption="Current annotations preview", use_column_width=True)

selected_type = st.selectbox(
    "Select cell type to annotate:",
    LABELS,
    index=LABELS.index(st.session_state.selected_type),
    key="selected_type",
)

selected_color_hex = LABEL_COLORS[selected_type]
st.write(f"**Drawing mode**: Annotate **{selected_type}** cells")

canvas_height = height
canvas_width = width

canvas_result = st_canvas(
    fill_color=f"rgba({hex_to_rgba(selected_color_hex, 75)[0]}, {hex_to_rgba(selected_color_hex, 75)[1]}, {hex_to_rgba(selected_color_hex, 75)[2]}, 0.3)",
    stroke_width=2,
    background_image=image,
    update_streamlit=True,
    height=canvas_height,
    width=canvas_width,
    drawing_mode="rect",
    key=f"canvas_{current_image_name}_{st.session_state.canvas_revision}",
)

if canvas_result.json_data is not None:
    canvas_objects = canvas_result.json_data.get("objects", [])
    ingest_new_canvas_objects(
        canvas_objects=canvas_objects,
        selected_type=selected_type,
        canvas_width=canvas_width,
        canvas_height=canvas_height,
        image_width=width,
        image_height=height,
    )

button_col1, button_col2, button_col3 = st.columns([1, 1, 4])
with button_col1:
    if st.button("Undo Last Box"):
        undo_last_box()
        persist_annotations_for_image(current_image_name)
        st.rerun()

with button_col2:
    if st.button("Clear All"):
        clear_all_boxes()
        persist_annotations_for_image(current_image_name)
        st.rerun()

st.markdown("---")

with button_col1:
    if st.button("Save Annotation"):
        persist_annotations_for_image(current_image_name)
        is_saved, message = save_pascal_voc(current_image_name, width, height, st.session_state.annotations)
        if is_saved:
            st.success(message)
            st.session_state.annotations = []
            refresh_annotation_counts()
            st.session_state.annotation_store[current_image_key(current_image_name)] = []
            st.session_state.canvas_object_count = 0
            st.session_state.canvas_revision += 1
            st.rerun()
        else:
            st.error(message)