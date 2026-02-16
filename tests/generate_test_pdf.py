"""Generate a test PDF that simulates a textbook with chapters, sections, and images."""

import sys
from pathlib import Path

import fitz  # PyMuPDF


def create_test_pdf(output_path: str):
    doc = fitz.open()

    # --- Page 1: Title page ---
    page = doc.new_page(width=595, height=842)
    page.insert_text(
        (150, 200), "Exploring Society",
        fontsize=28, fontname="helv", color=(0, 0, 0),
    )
    page.insert_text(
        (200, 260), "India and Beyond",
        fontsize=20, fontname="helv", color=(0.3, 0.3, 0.3),
    )
    page.insert_text(
        (230, 320), "By NCERT",
        fontsize=14, fontname="helv", color=(0, 0, 0),
    )

    # --- Page 2: Table of Contents ---
    page = doc.new_page(width=595, height=842)
    page.insert_text((200, 60), "Table of Contents", fontsize=20, fontname="hebo")
    page.insert_text((72, 120), "Chapter 1: Introduction — Why Social Science? ......... 3", fontsize=12, fontname="helv")
    page.insert_text((72, 150), "Chapter 2: Locating Places on the Earth .................. 5", fontsize=12, fontname="helv")

    # --- Page 3-4: Chapter 1 ---
    page = doc.new_page(width=595, height=842)
    page.insert_text(
        (72, 60), "Chapter 1: Introduction — Why Social Science?",
        fontsize=22, fontname="hebo",
    )
    page.insert_text(
        (72, 110), "What is Social Science?",
        fontsize=16, fontname="hebo",
    )
    body1 = (
        "Social Science is the study of human society and the relationships among individuals "
        "within those societies. It encompasses many fields including history, geography, "
        "political science, economics, and sociology. Understanding Social Science helps us "
        "make sense of the world around us and our place in it."
    )
    page.insert_textbox(
        fitz.Rect(72, 130, 525, 300),
        body1, fontsize=11, fontname="helv",
    )

    page.insert_text(
        (72, 320), "DID YOU KNOW?",
        fontsize=13, fontname="hebo",
    )
    page.insert_textbox(
        fitz.Rect(72, 340, 525, 400),
        "The term 'Social Science' was first used in the early 19th century. "
        "It has since grown into one of the most important areas of academic study worldwide.",
        fontsize=11, fontname="helv",
    )

    # Draw a simple rectangle as a "diagram"
    shape = page.new_shape()
    shape.draw_rect(fitz.Rect(150, 430, 450, 600))
    shape.finish(color=(0, 0, 0), fill=(0.9, 0.95, 1.0), width=1)
    shape.commit()
    page.insert_text(
        (200, 520), "Diagram: Branches of Social Science",
        fontsize=10, fontname="helv",
    )

    # Insert a small image (create a colored pixmap with alpha)
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 100, 80), 1)
    pix.set_rect(pix.irect, (80, 140, 200, 255))  # blue-ish fill (RGBA)
    page.insert_image(fitz.Rect(380, 650, 480, 730), pixmap=pix)

    # Page 4: continuation
    page = doc.new_page(width=595, height=842)
    page.insert_text(
        (72, 60), "Theme A — India and the World: Land and the People",
        fontsize=16, fontname="hebo",
    )
    body2 = (
        "This first theme includes the basics of the geographical world around us. "
        "We will study how different landforms shape the lives of people in India and "
        "across the globe. Geography is not just about maps — it is about understanding "
        "the relationship between humans and their environment."
    )
    page.insert_textbox(
        fitz.Rect(72, 90, 525, 250),
        body2, fontsize=11, fontname="helv",
    )

    page.insert_text(
        (72, 280), "LET'S EXPLORE",
        fontsize=13, fontname="hebo",
    )
    page.insert_textbox(
        fitz.Rect(72, 300, 525, 370),
        "Observe the picture above. List three different landforms you can identify. "
        "Discuss with your classmates how these landforms affect the lives of the people living there.",
        fontsize=11, fontname="helv",
    )

    page.insert_text(
        (72, 400), "Key Terms",
        fontsize=14, fontname="hebo",
    )
    page.insert_textbox(
        fitz.Rect(72, 420, 525, 500),
        "KEY TERM: Landform — A natural feature of the earth's surface. "
        "Examples include mountains, plateaus, plains, and valleys.",
        fontsize=11, fontname="helv",
    )

    # --- Page 5-6: Chapter 2 ---
    page = doc.new_page(width=595, height=842)
    page.insert_text(
        (72, 60), "Chapter 2: Locating Places on the Earth",
        fontsize=22, fontname="hebo",
    )
    page.insert_text(
        (72, 110), "Understanding Coordinates",
        fontsize=16, fontname="hebo",
    )
    body3 = (
        "Every place on Earth can be identified by two numbers: its latitude and longitude. "
        "Latitude measures how far north or south a place is from the equator. "
        "Longitude measures how far east or west a place is from the Prime Meridian."
    )
    page.insert_textbox(
        fitz.Rect(72, 130, 525, 280),
        body3, fontsize=11, fontname="helv",
    )

    # Another small image
    pix2 = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 120, 100), 1)
    pix2.set_rect(pix2.irect, (200, 180, 80, 255))  # greenish fill (RGBA)
    page.insert_image(fitz.Rect(200, 300, 400, 450), pixmap=pix2)

    page.insert_text((220, 470), "Figure: Globe with Grid Lines", fontsize=10, fontname="helv")

    # Page 6: exercises
    page = doc.new_page(width=595, height=842)
    page.insert_text(
        (72, 60), "Exercises",
        fontsize=16, fontname="hebo",
    )
    page.insert_text(
        (72, 100), "EXERCISE: Review Questions",
        fontsize=13, fontname="hebo",
    )
    exercises = (
        "1. What is the difference between latitude and longitude?\n"
        "2. Name the imaginary line that divides the Earth into Northern and Southern hemispheres.\n"
        "3. Why are coordinates important for navigation?\n"
        "4. Locate your city on a map and write down its approximate latitude and longitude."
    )
    page.insert_textbox(
        fitz.Rect(72, 120, 525, 350),
        exercises, fontsize=11, fontname="helv",
    )

    # Add bookmarks (TOC)
    toc = [
        [1, "Chapter 1: Introduction — Why Social Science?", 3],
        [2, "What is Social Science?", 3],
        [2, "Theme A — India and the World", 4],
        [1, "Chapter 2: Locating Places on the Earth", 5],
        [2, "Understanding Coordinates", 5],
        [2, "Exercises", 6],
    ]
    doc.set_toc(toc)

    doc.save(output_path)
    doc.close()
    print(f"Test PDF created at: {output_path}")


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).parent / "test_sample.pdf")
    create_test_pdf(out)
