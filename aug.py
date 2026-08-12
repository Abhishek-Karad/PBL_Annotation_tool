import os
import io
from dotenv import load_dotenv
from supabase import create_client, Client
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
IMAGE_BUCKET = "images"
ANNOTATION_BUCKET = "annotations"
MIN_BOX_SIZE = 5

# Supabase configuration.
# Locally these values are read from .env.
# On Streamlit Cloud, add the same keys under Settings -> Secrets.
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", st.secrets["SUPABASE_URL"])
SUPABASE_KEY = os.getenv("SUPABASE_KEY", st.secrets["SUPABASE_KEY"]) 

if not SUPABASE_URL or not SUPABASE_KEY:
    st.error("SUPABASE_URL and SUPABASE_KEY must be configured.")
    st.stop()

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

LABELS = ["RBC", "WBC", "Platelets"]

LABEL_COLORS = {
    "RBC": "#D0021B",
    "WBC": "#4A90E2",
    "Platelets": "#7ED321",
}



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
        "cached_canvas_background": None,
        "cached_canvas_background_revision": -1,
    }

    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = deepcopy(value)


# ------------------ IMAGE HELPERS ------------------
def get_image_files() -> list[str]:
    try:
        files = supabase.storage.from_(IMAGE_BUCKET).list()
    except Exception:
        return []

    image_extensions = (".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff")
    return sorted(
        [
            item["name"]
            for item in files
            if item.get("name", "").lower().endswith(image_extensions)
        ]
    )


def get_unannotated_images() -> list[str]:
    images = get_image_files()
    unannotated = []
    for image_name in images:
        xml_name = os.path.splitext(image_name)[0] + ".xml"
        try:
            supabase.storage.from_(ANNOTATION_BUCKET).download(xml_name)
        except Exception:
            unannotated.append(image_name)
    return unannotated


def load_annotations_from_xml(image_name: str, image_width: int, image_height: int) -> list[dict]:
    xml_name = os.path.splitext(image_name)[0] + ".xml"

    try:
        xml_bytes = supabase.storage.from_(ANNOTATION_BUCKET).download(xml_name)
        tree = ET.ElementTree(ET.fromstring(xml_bytes))
    except ET.ParseError:
        return []
    except Exception:
        return []

    loaded_annotations: list[dict] = []
    for object_node in tree.getroot().findall("object"):
        label = object_node.findtext("name", default=LABELS[0])
        bndbox = object_node.find("bndbox")
        if bndbox is None:
            continue

        try:
            annotation = {
                "label": label if label in LABELS else LABELS[0],
                "xmin": int(float(bndbox.findtext("xmin", default="0"))),
                "ymin": int(float(bndbox.findtext("ymin", default="0"))),
                "xmax": int(float(bndbox.findtext("xmax", default="0"))),
                "ymax": int(float(bndbox.findtext("ymax", default="0"))),
            }
        except ValueError:
            continue

        normalized = normalize_annotation(annotation, image_width, image_height)
        if normalized is not None:
            loaded_annotations.append(normalized)

    return loaded_annotations


def get_current_image_name(images: list[str]) -> str | None:
    if not images:
        return None

    st.session_state.current_image_index = max(0, min(st.session_state.current_image_index, len(images) - 1))
    return images[st.session_state.current_image_index]


def load_image(image_name: str) -> Image.Image:
    image_bytes = supabase.storage.from_(IMAGE_BUCKET).download(image_name)

    with Image.open(io.BytesIO(image_bytes)) as image:
        return image.convert("RGB").copy()


# ------------------ ANNOTATION STATE ------------------
def current_image_key(image_name: str) -> str:
    return image_name


def load_annotations_for_image(image_name: str, image_width: int, image_height: int) -> None:
    key = current_image_key(image_name)
    loaded_annotations = load_annotations_from_xml(image_name, image_width, image_height)
    if loaded_annotations:
        st.session_state.annotations = loaded_annotations
    else:
        st.session_state.annotations = deepcopy(st.session_state.annotation_store.get(key, []))
    st.session_state.canvas_object_count = 0
    refresh_annotation_counts()


def activate_image(image_name: str, image_width: int, image_height: int) -> None:
    previous_image = st.session_state.active_image_name
    if previous_image and previous_image != image_name:
        persist_annotations_for_image(previous_image)

    if previous_image != image_name:
        load_annotations_for_image(image_name, image_width, image_height)
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
    xml_content = prettify_xml(root).encode("utf-8")
    xml_name = os.path.splitext(image_name)[0] + ".xml"

    try:
        supabase.storage.from_(ANNOTATION_BUCKET).upload(
            xml_name,
            xml_content,
            {"content-type": "application/xml", "upsert": "true"},
        )
    except Exception as exc:
        return False, f"Failed to save annotation: {exc}"

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


# ------------------ STYLE (pill buttons) ------------------
def inject_wireframe_css() -> None:
    st.markdown(
        """
        <style>
        .stApp {
            background-color: #f5f5f5;
        }

        .wireframe-title,
        h2.wireframe-title,
        [data-testid="stMarkdownContainer"] h2.wireframe-title,
        [data-testid="stMarkdownContainer"] .wireframe-title {
            color: #000000 !important;
            -webkit-text-fill-color: #000000 !important;
            font-weight: 600;
            text-align: center;
            margin-bottom: 24px;
        }

        /* pill-shaped buttons, matching the wireframe (WBC / RBC / Platelets / Save) */
        div[data-testid="stButton"] > button {
            background-color: #ffffff;
            color: #1a1a1a;
            border: none;
            border-radius: 999px;
            padding: 0.75rem 1rem;
            font-size: 1rem;
            font-weight: 500;
            width: 100%;
            box-shadow: 0 1px 2px rgba(0, 0, 0, 0.15);
            transition: transform 0.05s ease-in-out;
        }

        div[data-testid="stButton"] > button:hover {
            background-color: #ffffff;
            color: #000000;
            border: none;
            transform: translateY(-1px);
        }

        div[data-testid="stButton"] > button:focus:not(:active) {
            box-shadow: 0 0 0 3px rgba(74, 144, 226, 0.5);
        }

        /* the selected cell-type button gets a colored ring */
        .label-selected div[data-testid="stButton"] > button {
            box-shadow: 0 0 0 3px var(--selected-ring, #4A90E2);
        }

        /* the "Counter : RBC: .. WBC: .. PLATELETS: .." pill */
        .counter-pill {
            background-color: #ffffff;
            border-radius: 999px;
            padding: 0.85rem 1.5rem;
            font-size: 1rem;
            color: #1a1a1a;
            box-shadow: 0 1px 2px rgba(0, 0, 0, 0.15);
            display: flex;
            align-items: center;
            height: 100%;
            white-space: nowrap;
        }

        .canvas-frame {
            display: flex;
            justify-content: center;
            margin-bottom: 28px;
        }

        .nav-row, .util-row {
            opacity: 0.9;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


# ------------------ STREAMLIT UI ------------------
st.set_page_config(layout="wide")
inject_wireframe_css()

initialize_session_state()

images = get_image_files()
if not images:
    st.success("All images annotated!")
    st.stop()

current_image_name = get_current_image_name(images)
if current_image_name is None:
    st.success("All images annotated!")
    st.stop()

image = load_image(current_image_name)
width, height = image.size

activate_image(current_image_name, width, height)

st.markdown(
    f'<h2 class="wireframe-title">Edge-AI Based Automated Hematology Screening System Using Digital Microscopy</h2>',
    unsafe_allow_html=True,
)
st.markdown(
    f'<p style="text-align:center; color:#2b2b2b; margin-top:-12px;">Annotating: <b>{current_image_name}</b> '
    f'({st.session_state.current_image_index + 1} / {len(images)})</p>',
    unsafe_allow_html=True,
)

# --- small, unobtrusive utility row: navigation + undo/clear (kept for full logic) ---
util_col1, util_col2, util_col3, util_col4 = st.columns([1, 1, 1, 1])
with util_col1:
    if st.button("◀ Previous", disabled=st.session_state.current_image_index <= 0, key="prev_btn"):
        persist_annotations_for_image(current_image_name)
        st.session_state.current_image_index -= 1
        st.rerun()
with util_col2:
    if st.button("Next ▶", disabled=st.session_state.current_image_index >= len(images) - 1, key="next_btn"):
        persist_annotations_for_image(current_image_name)
        st.session_state.current_image_index += 1
        st.rerun()
with util_col3:
    if st.button("Undo Last Box", key="undo_btn"):
        undo_last_box()
        persist_annotations_for_image(current_image_name)
        st.rerun()
with util_col4:
    if st.button("Clear All", key="clear_btn"):
        clear_all_boxes()
        persist_annotations_for_image(current_image_name)
        st.rerun()

st.write("")

# --- canvas (the "hero image" area from the wireframe) ---
canvas_height = height
canvas_width = width

selected_type = st.session_state.selected_type
selected_color_hex = LABEL_COLORS[selected_type]

# IMPORTANT: background_image must stay the SAME object across reruns that
# don't actually need a new one. load_image() re-reads and re-decodes the
# file into a brand-new PIL object every rerun (including reruns triggered
# just by drawing a box, since update_streamlit=True reruns the whole
# script on every canvas interaction). Even with identical bytes, handing
# the canvas component a new object every rerun races with Streamlit's
# media registration and can render as a blank/black canvas or throw
# "MediaFileHandler: Missing file". So we cache one background image per
# canvas_revision (which only bumps on image switch / undo / clear / save)
# and reuse that exact object for every rerun within the same revision —
# including the ones where you're actively drawing new boxes.
if st.session_state.cached_canvas_background_revision != st.session_state.canvas_revision:
    st.session_state.cached_canvas_background = draw_colored_boxes(image, st.session_state.annotations)
    st.session_state.cached_canvas_background_revision = st.session_state.canvas_revision

canvas_background = st.session_state.cached_canvas_background

canvas_center_left, canvas_center, canvas_center_right = st.columns([1, 3, 1])
with canvas_center:
    canvas_result = st_canvas(
        fill_color=f"rgba({hex_to_rgba(selected_color_hex, 75)[0]}, {hex_to_rgba(selected_color_hex, 75)[1]}, {hex_to_rgba(selected_color_hex, 75)[2]}, 0.3)",
        stroke_width=2,
        stroke_color=selected_color_hex,
        background_image=canvas_background,
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

st.write("")

# --- row 1: WBC / RBC / Platelets pill buttons (mirrors the wireframe layout) ---
label_col1, label_col2, label_spacer, label_col3 = st.columns([1, 1, 0.4, 1])
label_columns = {"WBC": label_col1, "RBC": label_col2, "Platelets": label_col3}

for label in ["WBC", "RBC", "Platelets"]:
    with label_columns[label]:
        is_selected = st.session_state.selected_type == label
        wrapper_class = "label-selected" if is_selected else ""
        st.markdown(
            f'<div class="{wrapper_class}" style="--selected-ring:{LABEL_COLORS[label]};">',
            unsafe_allow_html=True,
        )
        color_symbol = {"RBC": "🔴", "WBC": "🔵", "Platelets": "🟢"}[label]
        button_text = f"{color_symbol} {label}" if not is_selected else f"● {label}"
        if st.button(button_text, key=f"label_btn_{label}"):
            st.session_state.selected_type = label
            st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)

st.write("")

# --- row 2: counter pill (left, wide) + Save pill (right), matching the wireframe ---
counter_col, save_spacer, save_col = st.columns([2, 0.4, 1])
with counter_col:
    st.markdown(
        f'<div class="counter-pill">Counter : '
        f'RBC: {st.session_state.annotation_counts.get("RBC", 0)} &nbsp;&nbsp; '
        f'WBC: {st.session_state.annotation_counts.get("WBC", 0)} &nbsp;&nbsp; '
        f'PLATELETS: {st.session_state.annotation_counts.get("Platelets", 0)}'
        f'</div>',
        unsafe_allow_html=True,
    )
with save_col:
    if st.button("Save", key="save_btn"):
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