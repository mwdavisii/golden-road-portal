"""Pages. Autoescape is on for every template, including .txt includes: text from people, GitHub or a draft is never HTML."""

from pathlib import Path

import jinja2
from fastapi.responses import HTMLResponse

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
ENV = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES), autoescape=True, undefined=jinja2.StrictUndefined)


def page(request, name: str, status: int = 200, **context) -> HTMLResponse:
    from .access import Principal
    principal = getattr(request.state, "principal", None) or Principal()
    html = ENV.get_template(name).render(principal=principal, csrf=principal.csrf, settings=request.app.state.settings, **context)
    return HTMLResponse(html, status_code=status)
