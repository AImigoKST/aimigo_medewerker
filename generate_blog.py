"""
AI-migo Blog Generator — FastAPI backend
POST /generate  →  generates a full blog HTML from subject, picture, context
"""

import json
import os
import re
import shutil
from datetime import date
from pathlib import Path

import anthropic
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OUTPUT_DIR = Path("output")          # generated blogs land here: output/<slug>/
TEMPLATE_PATH = Path("blog-template.html")

app = FastAPI(title="AI-migo Blog Generator")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve the output folder so generated blogs are preview-able
OUTPUT_DIR.mkdir(exist_ok=True)
app.mount("/output", StaticFiles(directory=str(OUTPUT_DIR)), name="output")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[àáâãäå]", "a", text)
    text = re.sub(r"[èéêë]", "e", text)
    text = re.sub(r"[ìíîï]", "i", text)
    text = re.sub(r"[òóôõö]", "o", text)
    text = re.sub(r"[ùúûü]", "u", text)
    text = re.sub(r"[ý]", "y", text)
    text = re.sub(r"[ñ]", "n", text)
    text = re.sub(r"[ç]", "c", text)
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s]+", "-", text.strip())
    return text[:80]


def dutch_date(iso: str) -> str:
    months = [
        "", "januari", "februari", "maart", "april", "mei", "juni",
        "juli", "augustus", "september", "oktober", "november", "december",
    ]
    y, m, d = iso.split("-")
    return f"{int(d)} {months[int(m)]} {y}"


def build_prompt(subject: str, context: str, image_filename: str, slug: str) -> str:
    today = date.today().isoformat()

    with open("index.html", encoding="utf-8") as fh:
        reference_html = fh.read()

    return f"""Je bent een senior SEO-copywriter en content-specialist voor AI-migo (ai-migo.nl).
AI-migo bouwt AI-chatbots voor het Nederlandse MKB. De doelgroep zijn Nederlandse MKB-ondernemers
(webshops, dienstenbedrijven, retailers) die AI willen inzetten maar geen technische kennis hebben.

Schrijf een compleet, SEO-geoptimaliseerd blogartikel in het Nederlands over het volgende onderwerp:

ONDERWERP: {subject}
EXTRA CONTEXT / INSTRUCTIES: {context if context else "(geen extra context)"}
DATUM VANDAAG: {today}
SLUG: {slug}
AFBEELDINGSBESTANDSNAAM: {image_filename}

---

REFERENTIE-BLOG (exact deze structuur en stijl aanhouden):
{reference_html}

---

VEREISTEN:
1. Lengte: 1200–2000 woorden in de article-body (4–10 minuten leestijd). Schat de leestijd zelf (200 wpm).
2. Taal: volledig Nederlands, informele maar professionele toon (je/jij).
3. Structuur (verplicht in deze volgorde):
   - Intro-paragraaf met <strong>-accenten en een bronvermelding <span class="source">(...)</span>
   - 3 stat-kaarten in een <div class="stat-band"> met echte of aannemelijke statistieken
   - Meerdere H2-secties met inhoudelijke uitleg
   - Minimaal één <blockquote>-citaat (pakkende inzicht, geen directe quote van een persoon)
   - Minimaal één genummerde of ongenummerde lijst
   - Een <div class="faq"> met 4–5 <details>/<summary>-vragen
   - Een afsluitende paragraaf voor de <hr>
4. Interne links: verwijzing naar https://ai-migo.nl/#contact, https://ai-migo.nl/#pricing en https://ai-migo.nl/#home waar relevant.
5. Gebruik <span class="source">(...)</span> voor bronvermeldingen.
6. Gebruik GEEN markdown in de HTML-output — alleen raw HTML-tags.

---

Geef je antwoord UITSLUITEND als één geldig JSON-object met exact de volgende sleutels
(geen uitleg, geen markdown-codeblokken, alleen het JSON-object):

{{
  "meta_title": "...",
  "meta_description": "...",
  "meta_keywords": "...",
  "og_title": "...",
  "og_description": "...",
  "og_image_alt": "...",
  "schema_headline": "...",
  "article_section": "...",
  "eyebrow": "...",
  "h1": "...",
  "dek": "...",
  "read_time": 6,
  "image_alt": "...",
  "body_html": "...",
  "cta_title": "...",
  "cta_body": "...",
  "faq_schema": {{
    "@context": "https://schema.org",
    "@type": "FAQPage",
    "mainEntity": [
      {{
        "@type": "Question",
        "name": "...",
        "acceptedAnswer": {{ "@type": "Answer", "text": "..." }}
      }}
    ]
  }}
}}

Zorg dat body_html alle HTML bevat die tussen <div class="article-body"> en </div> hoort,
inclusief stat-band, blockquote, faq-sectie en de afsluitende <hr> + slotparagraaf.
"""


def fill_template(template: str, data: dict, slug: str, image_filename: str) -> str:
    today = date.today().isoformat()
    replacements = {
        "{{META_TITLE}}": data["meta_title"],
        "{{META_DESCRIPTION}}": data["meta_description"],
        "{{META_KEYWORDS}}": data["meta_keywords"],
        "{{OG_TITLE}}": data["og_title"],
        "{{OG_DESCRIPTION}}": data["og_description"],
        "{{OG_IMAGE_ALT}}": data["og_image_alt"],
        "{{SCHEMA_HEADLINE}}": data["schema_headline"],
        "{{ARTICLE_SECTION}}": data["article_section"],
        "{{DATE_ISO}}": today,
        "{{DATE_NL}}": dutch_date(today),
        "{{SLUG}}": slug,
        "{{IMAGE_FILENAME}}": image_filename,
        "{{IMAGE_ALT}}": data["image_alt"],
        "{{EYEBROW}}": data["eyebrow"],
        "{{H1}}": data["h1"],
        "{{DEK}}": data["dek"],
        "{{READ_TIME}}": str(data["read_time"]),
        "{{BODY_HTML}}": data["body_html"],
        "{{CTA_TITLE}}": data["cta_title"],
        "{{CTA_BODY}}": data["cta_body"],
        "{{FAQ_SCHEMA}}": json.dumps(data["faq_schema"], ensure_ascii=False, indent=2),
    }
    for token, value in replacements.items():
        template = template.replace(token, value)
    return template


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@app.post("/generate")
async def generate_blog(
    subject: str = Form(...),
    context: str = Form(""),
    picture: UploadFile = File(...),
):
    # 1. Slug + paths
    slug = slugify(subject)
    if not slug:
        raise HTTPException(status_code=400, detail="Ongeldig onderwerp voor slug.")

    blog_dir = OUTPUT_DIR / slug
    assets_dir = blog_dir / "assets"
    blog_dir.mkdir(parents=True, exist_ok=True)
    assets_dir.mkdir(exist_ok=True)

    # 2. Save uploaded image
    image_filename = picture.filename or "blog_picture.jpg"
    image_filename = re.sub(r"[^\w.\-]", "_", image_filename)
    image_path = assets_dir / image_filename
    with open(image_path, "wb") as fh:
        shutil.copyfileobj(picture.file, fh)

    # Also copy logo so the blog renders standalone
    logo_src = Path("assets/aimigo_logo.png")
    if logo_src.exists():
        shutil.copy(logo_src, assets_dir / "aimigo_logo.png")

    # 3. Call Claude
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = build_prompt(subject, context, image_filename, slug)

    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8192,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIError as exc:
        raise HTTPException(status_code=502, detail=f"Claude API fout: {exc}") from exc

    raw = message.content[0].text.strip()

    # Strip markdown code fences if Claude added them
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"```\s*$", "", raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Claude gaf geen geldig JSON terug: {exc}\n\nRaw output (eerste 500 tekens):\n{raw[:500]}",
        ) from exc

    # 4. Fill template
    template_html = TEMPLATE_PATH.read_text(encoding="utf-8")
    output_html = fill_template(template_html, data, slug, image_filename)

    # Fix relative asset paths so the standalone blog finds the logo
    output_html = output_html.replace(
        "https://ai-migo.nl/assets/aimigo_logo.png",
        "assets/aimigo_logo.png",
    )

    # 5. Write output
    output_path = blog_dir / "index.html"
    output_path.write_text(output_html, encoding="utf-8")

    return JSONResponse({
        "slug": slug,
        "preview_url": f"/output/{slug}/index.html",
        "output_path": str(output_path),
        "read_time": data.get("read_time"),
        "meta_title": data.get("meta_title"),
    })


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/")
def root():
    return {"status": "AI-migo Blog Generator draait. POST naar /generate."}


# ---------------------------------------------------------------------------
# Run directly: python generate_blog.py
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("generate_blog:app", host="0.0.0.0", port=8000, reload=True)
