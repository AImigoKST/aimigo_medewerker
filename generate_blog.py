"""
AI-migo Blog Generator — FastAPI backend
POST /generate  →  generates blog HTML, caches it, returns JSON
POST /publish   →  SFTPs cached blog to Strato
"""

import io
import json
import os
import re
from datetime import date
from pathlib import Path

import anthropic
import paramiko
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SFTP_HOST         = os.environ.get("SFTP_HOST", "")
SFTP_USER         = os.environ.get("SFTP_USER", "")
SFTP_PASS         = os.environ.get("SFTP_PASS", "")
SFTP_BLOG_PATH    = os.environ.get("SFTP_BLOG_PATH", "/www.ai-migo.nl/blog")
SFTP_PORT         = 22

TEMPLATE_PATH = Path("blog-template.html")

app = FastAPI(title="AI-migo Blog Generator")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory cache: slug → { html, image_bytes, image_filename }
# Safe on Render with WEB_CONCURRENCY=1 (single worker)
blog_cache: dict = {}


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
    text = re.sub(r"\s+", "-", text.strip())
    return text[:80]


def dutch_date(iso: str) -> str:
    months = [
        "", "januari", "februari", "maart", "april", "mei", "juni",
        "juli", "augustus", "september", "oktober", "november", "december",
    ]
    y, m, d = iso.split("-")
    return f"{int(d)} {months[int(m)]} {y}"


def build_prompt(subject: str, context: str, image_filename: str, slug: str,
                 include_stats: bool = False, include_faq: bool = False) -> str:
    today = date.today().isoformat()

    with open("index.html", encoding="utf-8") as fh:
        reference_html = fh.read()

    structure_lines = [
        "   - Intro-paragraaf met <strong>-accenten en een bronvermelding <span class=\"source\">(...)</span>",
    ]
    if include_stats:
        structure_lines.append(
            "   - 3 stat-kaarten in een <div class=\"stat-band\"> met echte of aannemelijke statistieken"
        )
    else:
        structure_lines.append("   - GEEN stat-band of statistiekenblok opnemen")

    structure_lines += [
        "   - Meerdere H2-secties met inhoudelijke uitleg",
        "   - Minimaal één <blockquote>-citaat (pakkende inzicht, geen directe quote van een persoon)",
        "   - Minimaal één genummerde of ongenummerde lijst",
    ]

    if include_faq:
        structure_lines.append(
            "   - Een <div class=\"faq\"> met 4–5 <details>/<summary>-vragen"
        )
    else:
        structure_lines.append("   - GEEN FAQ-sectie opnemen")

    structure_lines.append("   - Een afsluitende paragraaf voor de <hr>")
    structure_block = "\n".join(structure_lines)

    faq_schema_instruction = (
        """  "faq_schema": {
    "@context": "https://schema.org",
    "@type": "FAQPage",
    "mainEntity": [
      {
        "@type": "Question",
        "name": "...",
        "acceptedAnswer": { "@type": "Answer", "text": "..." }
      }
    ]
  }"""
        if include_faq else '  "faq_schema": null'
    )

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

REFERENTIE-BLOG (gebruik voor stijl, CSS-klassen en opmaak — maar volg de structuur hieronder):
{reference_html}

---

VEREISTEN:
1. Lengte: 1200–2000 woorden in de article-body (4–10 minuten leestijd). Schat de leestijd zelf (200 wpm).
2. Taal: volledig Nederlands, informele maar professionele toon (je/jij).
3. Structuur (verplicht in deze volgorde):
{structure_block}
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
{faq_schema_instruction}
}}

Zorg dat body_html alle HTML bevat die tussen <div class="article-body"> en </div> hoort,
inclusief de afsluitende <hr> + slotparagraaf.
"""


def fill_template(template: str, data: dict, slug: str, image_filename: str) -> str:
    today = date.today().isoformat()
    faq_schema_value = data.get("faq_schema")
    faq_schema_json  = json.dumps(faq_schema_value, ensure_ascii=False, indent=2) if faq_schema_value else "{}"

    replacements = {
        "{{META_TITLE}}":       data["meta_title"],
        "{{META_DESCRIPTION}}": data["meta_description"],
        "{{META_KEYWORDS}}":    data["meta_keywords"],
        "{{OG_TITLE}}":         data["og_title"],
        "{{OG_DESCRIPTION}}":   data["og_description"],
        "{{OG_IMAGE_ALT}}":     data["og_image_alt"],
        "{{SCHEMA_HEADLINE}}":  data["schema_headline"],
        "{{ARTICLE_SECTION}}":  data["article_section"],
        "{{DATE_ISO}}":         today,
        "{{DATE_NL}}":          dutch_date(today),
        "{{SLUG}}":             slug,
        "{{IMAGE_FILENAME}}":   image_filename,
        "{{IMAGE_ALT}}":        data["image_alt"],
        "{{EYEBROW}}":          data["eyebrow"],
        "{{H1}}":               data["h1"],
        "{{DEK}}":              data["dek"],
        "{{READ_TIME}}":        str(data["read_time"]),
        "{{BODY_HTML}}":        data["body_html"],
        "{{CTA_TITLE}}":        data["cta_title"],
        "{{CTA_BODY}}":         data["cta_body"],
        "{{FAQ_SCHEMA}}":       faq_schema_json,
    }
    for token, value in replacements.items():
        template = template.replace(token, value)
    return template


def sftp_mkdir_p(sftp: paramiko.SFTPClient, remote_path: str):
    """Create remote directory and every missing parent, like mkdir -p."""
    parts = remote_path.strip("/").split("/")
    current = ""
    for part in parts:
        current += f"/{part}"
        try:
            sftp.mkdir(current)
        except OSError:
            pass  # already exists — continue


def sftp_upload(slug: str, html: str, image_bytes: bytes, image_filename: str):
    """Upload index.html + assets to Strato via SFTP."""
    transport = paramiko.Transport((SFTP_HOST, SFTP_PORT))
    transport.connect(username=SFTP_USER, password=SFTP_PASS)
    sftp = paramiko.SFTPClient.from_transport(transport)

    blog_dir   = f"{SFTP_BLOG_PATH}/{slug}"
    assets_dir = f"{blog_dir}/assets"

    # Create full directory tree, parent by parent
    sftp_mkdir_p(sftp, blog_dir)
    sftp_mkdir_p(sftp, assets_dir)

    # Upload index.html (write as bytes for reliability)
    with sftp.open(f"{blog_dir}/index.html", "wb") as fh:
        fh.write(html.encode("utf-8"))

    # Upload blog image
    with sftp.open(f"{assets_dir}/{image_filename}", "wb") as fh:
        fh.write(image_bytes)

    # Upload logo
    logo_path = Path("assets/aimigo_logo.png")
    if logo_path.exists():
        sftp.put(str(logo_path), f"{assets_dir}/aimigo_logo.png")

    sftp.close()
    transport.close()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/generate")
async def generate_blog(
    subject: str = Form(...),
    context: str = Form(""),
    picture: UploadFile = File(...),
    include_stats: str = Form(""),
    include_faq: str = Form(""),
):
    slug = slugify(subject)
    if not slug:
        raise HTTPException(status_code=400, detail="Ongeldig onderwerp voor slug.")

    want_stats = include_stats == "1"
    want_faq   = include_faq == "1"

    image_bytes    = await picture.read()
    image_filename = re.sub(r"[^\w.\-]", "_", picture.filename or "blog_picture.jpg")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = build_prompt(subject, context, image_filename, slug, want_stats, want_faq)

    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8192,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIError as exc:
        raise HTTPException(status_code=502, detail=f"Claude API fout: {exc}") from exc

    raw = message.content[0].text.strip()
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"```\s*$", "", raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Claude gaf geen geldig JSON terug: {exc}\n\nRaw output:\n{raw[:500]}",
        ) from exc

    template_html = TEMPLATE_PATH.read_text(encoding="utf-8")
    output_html   = fill_template(template_html, data, slug, image_filename)

    # Cache for /publish
    blog_cache[slug] = {
        "html":           output_html,
        "image_bytes":    image_bytes,
        "image_filename": image_filename,
        "read_time":      data.get("read_time", "?"),
    }

    return JSONResponse({
        "slug":      slug,
        "html":      output_html,
        "read_time": data.get("read_time", "?"),
    })


@app.post("/publish")
async def publish_blog(slug: str = Form(...)):
    entry = blog_cache.get(slug)
    if not entry:
        raise HTTPException(
            status_code=404,
            detail="Blog niet gevonden in cache. Genereer de blog opnieuw en probeer dan te publiceren.",
        )

    if not SFTP_HOST or not SFTP_USER or not SFTP_PASS:
        raise HTTPException(status_code=500, detail="SFTP-instellingen ontbreken op de server.")

    try:
        sftp_upload(
            slug           = slug,
            html           = entry["html"],
            image_bytes    = entry["image_bytes"],
            image_filename = entry["image_filename"],
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"SFTP-upload mislukt: {exc}") from exc

    # Remove from cache after successful publish
    blog_cache.pop(slug, None)

    return JSONResponse({
        "url": f"https://ai-migo.nl/blog/{slug}/",
    })


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/")
def root():
    return {"status": "AI-migo Blog Generator draait. POST naar /generate of /publish."}


# ---------------------------------------------------------------------------
# Run directly: python generate_blog.py
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("generate_blog:app", host="0.0.0.0", port=8000, reload=True)
