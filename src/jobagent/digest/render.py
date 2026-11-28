"""Render a Digest to email-safe HTML (templates/digest.html.j2).

Email-safe: inline CSS only, tables for layout, a 600px column that shrinks on phones, no images
or external CSS. Autoescape is on: titles and reasons are untrusted text. Links come from source
data (DigestLine.url), never from the LLM.
"""

from pathlib import Path

from jinja2 import Environment, PackageLoader, StrictUndefined

from jobagent.digest.select import Digest

OUT_PATH = Path("out/digest.html")  # gitignored
BADGE_COLORS = {"High": "#15803d", "Medium": "#b45309", "Low": "#71717a"}

_ENV = Environment(
    loader=PackageLoader("jobagent.digest", "templates"),
    autoescape=True,
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)


def badge_color(fit: str) -> str:
    return BADGE_COLORS.get(fit, BADGE_COLORS["Low"])


def days_text(days: int | None) -> str:
    if days is None:
        return "posted date n/a"
    if days == 0:
        return "today"
    return "1 day ago" if days == 1 else f"{days} days ago"


def render_text(digest: Digest) -> str:
    """The plain-text part of the email: the same lines, no reasons for compact rows."""
    out = [digest.subject, f"Job digest, {digest.today:%a %d %b %Y}", ""]
    for section in digest.sections:
        out += [f"{section.name} ({section.count})", "=" * len(section.name)]
        for line in section.full:
            tags = " [rule score]" if line.rule else ""
            star = "* " if line.star else ""
            out += [
                f"{line.score} {line.fit}{tags}  {star}{line.company} - {line.title}",
                f"    {line.location} | {line.yoe} | {days_text(line.days_posted)}",
                f"    {line.reason}",
                f"    {line.url}",
                "",
            ]
        if section.compact:
            out.append(f"More Medium matches ({len(section.compact)}):")
            out += [
                f"  {line.score}{' [rule score]' if line.rule else ''}  {'* ' if line.star else ''}"
                f"{line.company} - {line.title} | {line.location} | {line.url}"
                for line in section.compact
            ]
            out.append("")
    if any(s.stars for s in digest.sections):
        out.append("* company with a US transfer path")
    out.append("Scores: 75+ High, 50-74 Medium. Links come from the job boards.")
    return "\n".join(out) + "\n"


def render_digest(digest: Digest) -> str:
    template = _ENV.get_template("digest.html.j2")
    return template.render(digest=digest, badge_color=badge_color, days_text=days_text)


def write_digest(digest: Digest, path: Path = OUT_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_digest(digest), encoding="utf-8")
    return path
