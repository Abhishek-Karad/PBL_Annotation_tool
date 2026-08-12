import os
import streamlit as st
from PIL import Image, ImageDraw
import xml.etree.ElementTree as ET
from streamlit_drawable_canvas import st_canvas

# ------------------ CONFIG ------------------
IMAGE_DIR = "images"
ANNOTATION_DIR = "annotations"

LABELS = ["RBC", "WBC", "Platelets"]

# Color mapping for each class (from meta.json)
LABEL_COLORS = {
    "RBC": "#D0021B",      # Red
    "WBC": "#4A90E2",      # Blue
    "Platelets": "#7ED321" # Green
}

os.makedirs(ANNOTATION_DIR, exist_ok=True)

# ------------------ DRAW COLORED BOXES ------------------
def draw_colored_boxes(image, boxes):
    """Draw bounding boxes with label-specific colors on the image"""
    img_copy = image.copy()
    draw = ImageDraw.Draw(img_copy, "RGBA")
    
    for box in boxes:
        label = box["label"]
        color_hex = LABEL_COLORS.get(label, "#FFFFFF")
        # Convert hex to RGB + alpha
        color_rgb = tuple(int(color_hex.lstrip('#')[i:i+2], 16) for i in (0, 2, 4))
        color_rgba = color_rgb + (100,)  # Add alpha transparency
        
        # Draw filled rectangle
        draw.rectangle(
            [(box["xmin"], box["ymin"]), (box["xmax"], box["ymax"])],
            fill=color_rgba,
            outline=color_hex,
            width=3
        )

    return img_copy

# ------------------ LOAD IMAGES ------------------
def get_unannotated_images():
    images = [f for f in os.listdir(IMAGE_DIR) if f.lower().endswith((".jpg", ".jpeg", ".png"))]
    unannotated = []

    for img in images:
        xml_name = os.path.splitext(img)[0] + ".xml"
        if not os.path.exists(os.path.join(ANNOTATION_DIR, xml_name)):
            unannotated.append(img)

    return sorted(unannotated)

# ------------------ SAVE XML ------------------
def save_pascal_voc(image_name, image_size, boxes):
    annotation = ET.Element("annotation")

    ET.SubElement(annotation, "filename").text = image_name

    size = ET.SubElement(annotation, "size")
    ET.SubElement(size, "width").text = str(image_size[0])
    ET.SubElement(size, "height").text = str(image_size[1])
    ET.SubElement(size, "depth").text = "3"

    for box in boxes:
        obj = ET.SubElement(annotation, "object")
        ET.SubElement(obj, "name").text = box["label"]

        bndbox = ET.SubElement(obj, "bndbox")
        ET.SubElement(bndbox, "xmin").text = str(int(box["xmin"]))
        ET.SubElement(bndbox, "ymin").text = str(int(box["ymin"]))
        ET.SubElement(bndbox, "xmax").text = str(int(box["xmax"]))
        ET.SubElement(bndbox, "ymax").text = str(int(box["ymax"]))

    tree = ET.ElementTree(annotation)
    xml_path = os.path.join(ANNOTATION_DIR, os.path.splitext(image_name)[0] + ".xml")
    tree.write(xml_path)

# ------------------ STREAMLIT UI ------------------
st.set_page_config(layout="wide")
st.title("Edge-AI Based Automated Hematology Screening System Using Digital Microscopy")

# Initialize session state for storing all boxes
if "all_boxes" not in st.session_state:
    st.session_state.all_boxes = []

images = get_unannotated_images()

if len(images) == 0:
    st.success("All images annotated!")
    st.stop()

# Current image
current_image_name = images[0]
image_path = os.path.join(IMAGE_DIR, current_image_name)

image = Image.open(image_path)
width, height = image.size

st.subheader(f"Annotating: {current_image_name}")

# Show color legend upfront
st.markdown("**Color Legend:**")
col_legend1, col_legend2, col_legend3 = st.columns(3)
with col_legend1:
    st.markdown(f"Red = **RBC**")
with col_legend2:
    st.markdown(f"Blue = **WBC**")
with col_legend3:
    st.markdown(f"Green = **Platelets**")

st.markdown("---")

# Show predicted counts (placeholder values - can be updated from input/model)
st.markdown("### Predicted Cell Counts")
pred_col1, pred_col2, pred_col3 = st.columns(3)
with pred_col1:
    st.metric("RBC Count", 12)
with pred_col2:
    st.metric("WBC Count", 8)
with pred_col3:
    st.metric("Platelets Count", 15)

st.markdown("---")

# Select cell type to annotate
selected_type = st.selectbox(
    "Select cell type to annotate:",
    LABELS,
    key="selected_type"
)

# Get color for selected type
selected_color_hex = LABEL_COLORS[selected_type]
# Convert to RGBA for canvas
color_rgb = tuple(int(selected_color_hex.lstrip('#')[i:i+2], 16) for i in (0, 2, 4))
fill_color = f"rgba({color_rgb[0]}, {color_rgb[1]}, {color_rgb[2]}, 0.3)"

st.write(f"**Drawing mode**: Annotate **{selected_type}** cells")

# Canvas (increased size 5x)
canvas_result = st_canvas(
    fill_color=fill_color,
    stroke_width=2,
    background_image=image,
    update_streamlit=True,
    height=int(height * 2.5),
    width=int(width * 2.5),
    drawing_mode="rect",
    key="canvas",
)

# Process drawn boxes
if canvas_result.json_data is not None:
    objects = canvas_result.json_data.get("objects", [])

    if len(objects) > 0:
        # Add new boxes from current drawing
        for obj in objects:
            box = {
                "label": selected_type,
                "xmin": obj["left"],
                "ymin": obj["top"],
                "xmax": obj["left"] + obj["width"],
                "ymax": obj["top"] + obj["height"]
            }
            # Check if this box already exists
            if box not in st.session_state.all_boxes:
                st.session_state.all_boxes.append(box)


# Save button
col1, col2 = st.columns([1, 1])
with col1:
    if st.button("Save Annotation"):
        if len(st.session_state.all_boxes) == 0:
            st.warning("Please annotate at least one bounding box.")
        else:
            save_pascal_voc(current_image_name, (width, height), st.session_state.all_boxes)
            st.success(f"Annotation saved for {current_image_name}")
            st.session_state.all_boxes = []
            st.rerun()

with col2:
    if st.button("Clear All"):
        st.session_state.all_boxes = []
        st.rerun()

is theere any prb in this ?