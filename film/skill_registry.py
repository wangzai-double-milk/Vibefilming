"""Discover independent skills from frontmatter without loading agent or model clients."""
from pathlib import Path, PurePosixPath
import re


def frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    lines = text[3:end].splitlines()
    values = {}
    index = 0
    while index < len(lines):
        match = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", lines[index])
        if not match:
            index += 1
            continue
        key, raw = match.groups()
        raw = raw.strip()
        index += 1
        if re.fullmatch(r"[>|][+-]?", raw):
            chunks = []
            while index < len(lines) and (
                not lines[index].strip() or lines[index].startswith((" ", "\t"))
            ):
                chunks.append(lines[index].strip())
                index += 1
            raw = " ".join(chunk for chunk in chunks if chunk)
        values[key] = raw.strip("\"'")
    return values


def discover(skills_dir: Path, runtime: str = "local") -> list[dict]:
    root = Path(skills_dir).resolve()
    records, names = [], set()
    for skill_md in sorted(root.glob("*/SKILL.md")):
        if skill_md.is_symlink() or skill_md.parent.is_symlink():
            raise ValueError(f"Skill must stay inside skills directory: {skill_md}")
        metadata = frontmatter(skill_md.read_text(encoding="utf-8"))
        target_runtime = metadata.get("runtime", "").lower()
        if target_runtime not in {"", "all", runtime}:
            continue
        name = metadata.get("name", "")
        description = metadata.get("description", "")
        if not name or not description:
            continue
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
            raise ValueError(f"Invalid Skill name: {name!r}")
        if name in names:
            raise ValueError(f"Duplicate Skill name: {name}")
        names.add(name)
        record = {
            "name": name, "description": description,
            "path": f"skills/{skill_md.parent.name}/SKILL.md",
            "runtime": target_runtime,
        }
        for field in ("entrypoint", "requirements"):
            value = metadata.get(field, "")
            if not value:
                continue
            relative = PurePosixPath(value)
            target = skill_md.parent.joinpath(*relative.parts)
            if (relative.is_absolute() or "\\" in value or ".." in relative.parts
                    or not target.resolve().is_relative_to(skill_md.parent.resolve())
                    or not target.is_file()):
                raise ValueError(f"Invalid Skill {field}: {name}:{value}")
            record[field] = f"skills/{skill_md.parent.name}/{relative.as_posix()}"
        records.append(record)
    return records
