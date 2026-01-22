import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urllib_request

import yaml
from mkdocs.config.config_options import Type
from mkdocs.plugins import BasePlugin
from mkdocs.utils import log

# Module scope regex variables

FM_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
PLACEHOLDER_PATTERN = re.compile(r"{{\s*([A-Za-z0-9_.-]+)\s*}}")
SNIPPET_TOKEN_REGEX = re.compile(r"-{1,}8<-{2,}\s*['\"]([^'\"]+)['\"]")
SNIPPET_LINE_REGEX = re.compile(
    r"(?m)^(?P<indent>[ \t]*)-{1,}8<-{2,}\s*['\"](?P<ref>[^'\"]+)['\"]\s*$"
)
HEADING_RE = re.compile(r"^(#{2,6})\s+(.+?)\s*#*\s*$")
SNIPPET_SECTION_REGEX = re.compile(
    r"""^\s*(?:#|//|;|<!--)?\s*--8<--\s*\[(?P<kind>start|end):(?P<name>[^\]]+)\]\s*(?:-->)*\s*$""",
    re.IGNORECASE,
)
SNIPPET_DOUBLE_RANGE_RE = re.compile(r"^(?P<path>.+?)::(?P<end>-?\d+)$")
SNIPPET_RANGE_RE = re.compile(r"^(?P<path>.+?):(?P<start>-?\d+):(?P<end>-?\d+)$")
SNIPPET_SINGLE_RANGE_RE = re.compile(r"^(?P<path>.+?):(?P<start>-?\d+)$")

# Define plugin class
class ResolveMDPlugin(BasePlugin):
    # Define value for `llms_config` in the project mkdocs.yml file
    config_scheme = (("llms_config", Type(str, required=True)),)

    def __init__(self):
        super().__init__()
        self.allow_remote_snippets = True
        self.docs_base_url = "/"

    # Process will start after site build is complete
    def on_post_build(self, config):
        # Locate and load config
        config_file_path = Path(config["config_file_path"]).resolve()
        project_root = config_file_path.parent
        self.llms_config = self.load_llms_config(project_root)
        snippet_cfg = self.llms_config.get("snippets", {})
        self.allow_remote_snippets = snippet_cfg.get("allow_remote", True)

        # Resolve docs_dir from MkDocs config (already parsed/resolved by MkDocs)
        docs_dir = Path(config["docs_dir"]).resolve()
        site_dir = Path(config["site_dir"]).resolve()

        # Snippet directory defaults to docs/.snippets
        snippet_dir = docs_dir / ".snippets"
        if not snippet_dir.exists():
            log.debug(f"[resolve_md] snippet directory not found at {snippet_dir}")

        # Load shared variables (variables.yml sits inside docs_dir)
        variables_path = docs_dir / "variables.yml"
        variables = self.load_yaml(str(variables_path))
        if not variables:
            log.warning(f"[resolve_md] no variables loaded from {variables_path}")

        # Determine docs_base_url for canonical URLs
        project_cfg = self.llms_config.get("project", {})
        docs_base_url = (project_cfg.get("docs_base_url", "") or "").rstrip("/") + "/"
        self.docs_base_url = docs_base_url

        # Determine output directory for resolved markdown (inside site directory)
        ai_pages_dir = self.get_ai_output_dir(site_dir)
        ai_root = ai_pages_dir.parent
        ai_root.mkdir(parents=True, exist_ok=True)
        self.reset_directory(ai_pages_dir)
        log.info(f"[resolve_md] writing resolved pages to {ai_pages_dir}")

        # Loop through docs_dir MD files, filter for exclusions defined in llms_config.json
        content_cfg = self.llms_config.get("content", {})
        exclusions = content_cfg.get("exclusions", {})
        skip_basenames = exclusions.get("skip_basenames", [])
        skip_paths = exclusions.get("skip_paths", [])
        markdown_files = self.get_all_markdown_files(
            docs_dir, skip_basenames, skip_paths
        )

        log.info(f"[resolve_md] found {len(markdown_files)} markdown files")

        processed = 0

        ai_pages: list[dict] = []

        # For each file in markdown_files
        for md_path in markdown_files:
            text = Path(md_path).read_text(encoding="utf-8")
            # Separate, filter, map, and return desired front matter
            front_matter, body = self.split_front_matter(text)
            reduced_fm = self.map_front_matter(front_matter)
            categories = self.normalize_categories(reduced_fm.get("categories"))
            if categories:
                reduced_fm["categories"] = categories
            elif "categories" in reduced_fm:
                reduced_fm.pop("categories")
            # Resolve snippet placeholders first
            snippet_body = self.replace_snippet_placeholders(
                body, snippet_dir, variables
            )
            if snippet_body != body:
                log.debug(f"[resolve_md] resolved snippets in {md_path}")
            body = snippet_body
            # Resolve variable placeholders against variables.yml definitions
            resolved_body = self.resolve_markdown_placeholders(body, variables)
            if resolved_body != body:
                log.debug(f"[resolve_md] resolved placeholders in {md_path}")
            # Remove HTML comments after substitutions
            cleaned_body = self.remove_html_comments(resolved_body)
            if cleaned_body != resolved_body:
                log.debug(f"[resolve_md] stripped HTML comments in {md_path}")
            # Convert path to slug and canonical URLs
            rel_path = Path(md_path).relative_to(docs_dir)
            rel_no_ext = str(rel_path.with_suffix(""))
            slug, url = self.compute_slug_and_url(rel_no_ext, docs_base_url)
            # Calculate word count and estimated token count
            word_count = self.word_count(cleaned_body)
            token_estimate = self.estimate_tokens(cleaned_body)

            # Output resolved Markdown file to AI artifacts directory
            header = dict(reduced_fm)
            header["url"] = url
            header["word_count"] = word_count
            header["token_estimate"] = token_estimate
            self.write_ai_page(ai_pages_dir, slug, header, cleaned_body)
            processed += 1
            # Creates list used later for category file creation
            cats = reduced_fm.get("categories") or []
            if isinstance(cats, str):
                cats_value = [cats]
            else:
                cats_value = cats

            ai_pages.append(
                {
                    "slug": slug,
                    "path": Path(md_path),
                    "title": header.get("title") or slug,
                    "description": header.get("description") or "",
                    "categories": cats_value,
                    "url": url,
                    "word_count": word_count,
                    "token_estimate": token_estimate,
                    "body": cleaned_body,
                }
            )

            log.debug(f"[resolve_md] {md_path} FM keys: {list(front_matter.keys())}")
            log.debug(f"[resolve_md] {md_path} mapped FM: {reduced_fm}")

        log.info(f"[resolve_md] processed {processed} AI pages")
        if ai_pages:
            log.debug(
                f"[resolve_md] sample AI page metadata: slug={ai_pages[0]['slug']} cats={ai_pages[0].get('categories')}"
            )

        # Build category bundles based on current AI pages
        self.build_category_bundles(ai_pages, ai_root)
        # Build site index + llms JSONL artifacts
        self.build_site_index(ai_pages, ai_root)
        # Build llms.txt for downstream LLM usage directly into the site output
        self.build_llms_txt(ai_pages, site_dir)

    # ----- Helper functions -------

    # File discovery and filtering per skip names/paths in llms_config.json
    @staticmethod
    def get_all_markdown_files(docs_dir, skip_basenames, skip_paths):
        """Collect *.md|*.mdx, skipping basenames and paths that contain any skip_paths substring."""
        results = []
        for root, _, files in os.walk(docs_dir):
            if any(x in root for x in skip_paths):
                continue
            for file in files:
                if file.endswith((".md", ".mdx")) and file not in skip_basenames:
                    results.append(os.path.join(root, file))
        return sorted(results)

    # Loaders for: llms_config.json, yaml files, and Mkdocs docs_dir

    def load_llms_config(self, project_root: Path) -> dict:
        """Load llms_config.json from the repo root."""
        config_json = self.config["llms_config"]
        llms_config_path = (project_root / config_json).resolve()

        if not llms_config_path.exists():
            raise FileNotFoundError(f"llms_config not found at {llms_config_path}")

        with llms_config_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        log.debug(f"[resolve_md] llms_config keys: {list(data.keys())}")
        return data

    @staticmethod
    def load_yaml(yaml_file: str):
        """Load a YAML file; return {} if missing/empty."""
        if not os.path.exists(yaml_file):
            return {}
        try:
            with open(yaml_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            log.warning(f"[resolve_md] unable to parse YAML file {yaml_file}: {exc}")
            return {}
        return data or {}

    # Front-matter helpers

    @staticmethod
    def split_front_matter(source_text: str):
        """
        Return (front_matter_dict, body_text). If no FM, dict={} and body=source_text.
        """
        m = FM_PATTERN.match(source_text)
        if not m:
            return {}, source_text
        fm_text = m.group(1)
        try:
            fm = yaml.safe_load(fm_text) or {}
        except Exception:
            fm = {}
        body = source_text[m.end() :]
        return fm, body

    @staticmethod
    def map_front_matter(fm: dict) -> dict:
        """
        Emit front matter fields:
        - title (as-is)
        - description (as-is)
        - categories (as-is)
        """
        out = {}
        if "title" in fm:
            out["title"] = fm["title"]
        if "description" in fm:
            out["description"] = fm["description"]
        if "categories" in fm:
            out["categories"] = fm["categories"]
        return out

    @staticmethod
    def normalize_categories(raw) -> list[str]:
        """Normalize categories value into a list of non-empty strings."""
        if raw is None:
            return []
        result: list[str] = []
        if isinstance(raw, list):
            candidates = raw
        elif isinstance(raw, str):
            val = raw.strip()
            if not val:
                return []
            if val.startswith("[") and val.endswith("]"):
                try:
                    parsed = yaml.safe_load(val)
                    candidates = parsed if isinstance(parsed, list) else [val]
                except Exception:
                    candidates = [val]
            else:
                candidates = val.split(",")
        else:
            candidates = [raw]
        for item in candidates:
            text = str(item).strip()
            if text:
                result.append(text)
        return result

    # Resolve variables and placeholders

    @staticmethod
    def get_value_from_path(data, path):
        """Simple dotted lookup (dicts only, no arrays)."""
        if not path:
            return None
        keys = [k.strip() for k in path.split(".") if k.strip()]
        value = data
        for key in keys:
            if not isinstance(value, dict) or key not in value:
                return None
            value = value[key]
        return value

    @staticmethod
    def resolve_markdown_placeholders(content: str, variables: dict) -> str:
        """Replace {{ dotted.keys }} using variables dict; leave unknowns intact."""

        def replacer(match):
            key_path = match.group(1)
            value = ResolveMDPlugin.get_value_from_path(variables, key_path)
            return str(value) if value is not None else match.group(0)

        return PLACEHOLDER_PATTERN.sub(replacer, content)

    # Replace snippet placeholders with code blocks

    @staticmethod
    def parse_line_range(snippet_path: str):
        """Split snippet reference into filename and optional start/end line numbers."""
        ref = snippet_path.strip()
        if not ref:
            return "", None, None, None

        double_match = SNIPPET_DOUBLE_RANGE_RE.match(ref)
        if double_match:
            file_only = double_match.group("path")
            line_end = ResolveMDPlugin._parse_line_number(double_match.group("end"))
            return file_only, 1, line_end, None

        range_match = SNIPPET_RANGE_RE.match(ref)
        if range_match:
            file_only = range_match.group("path")
            line_start = ResolveMDPlugin._parse_line_number(range_match.group("start"))
            line_end = ResolveMDPlugin._parse_line_number(range_match.group("end"))
            return file_only, line_start, line_end, None

        single_match = SNIPPET_SINGLE_RANGE_RE.match(ref)
        if single_match:
            file_only = single_match.group("path")
            line_start = ResolveMDPlugin._parse_line_number(single_match.group("start"))
            return file_only, line_start, None, None

        idx = ResolveMDPlugin._find_selector_colon(ref)
        if idx is not None:
            section = ref[idx + 1 :].strip()
            file_only = ref[:idx]
            return file_only, None, None, section

        return ref, None, None, None

    @staticmethod
    def _parse_line_number(value: str) -> int | None:
        """Parse a signed integer string; return None if invalid/empty."""
        if value is None:
            return None
        text = value.strip()
        if not text:
            return None
        if text[0] in "+-":
            sign = -1 if text[0] == "-" else 1
            digits = text[1:]
            if not digits.isdigit():
                return None
            return sign * int(digits)
        if text.isdigit():
            return int(text)
        return None

    @staticmethod
    def _find_selector_colon(reference: str) -> int | None:
        """Return index of the last ':' separator that is not part of a scheme like 'http://'."""
        for idx in range(len(reference) - 1, -1, -1):
            if reference[idx] != ":":
                continue
            # Skip if part of '://'
            if reference[idx : idx + 3] == "://":
                continue
            # Skip Windows drive letters (:'\' or :'/')
            if idx + 1 < len(reference) and reference[idx + 1] in ("/", "\\"):
                continue
            # Skip double-colon sequences (handled elsewhere)
            if idx > 0 and reference[idx - 1] == ":":
                continue
            return idx
        return None

    def apply_snippet_selectors(
        self,
        content: str,
        line_start: int | None,
        line_end: int | None,
        section: str | None,
        snippet_ref: str,
    ) -> str | None:
        """Apply section or line slicing to snippet content."""
        selected = content
        if section:
            section_content = self.extract_snippet_section(selected, section, snippet_ref)
            if section_content is None:
                return None
            selected = section_content
        if line_start is not None or line_end is not None:
            lines = selected.split("\n")
            total = len(lines)
            if total == 0:
                return ""
            start_idx = self._normalize_line_index(line_start, total, default=1)
            end_idx = self._normalize_line_index(line_end, total, default=total)
            if end_idx < start_idx:
                log.warning(
                    f"[resolve_md] invalid line range ({line_start}:{line_end}) in {snippet_ref}"
                )
                return ""
            selected = "\n".join(lines[start_idx - 1 : end_idx])
        return selected

    @staticmethod
    def _normalize_line_index(index: int | None, total: int, default: int) -> int:
        """Convert possible negative/zero indexes into 1-based inclusive bounds."""
        if index is None:
            value = default
        else:
            value = index
            if value == 0:
                value = 1
            if value < 0:
                value = total + value + 1
        value = max(1, min(value, total)) if total else 1
        return value

    @staticmethod
    def extract_snippet_section(content: str, section: str, snippet_ref: str) -> str | None:
        """Return the text between --8<-- [start:section] and [end:section] markers."""
        target = section.strip().lower()
        if not target:
            return None
        lines = content.split("\n")
        start_idx = None
        for idx, line in enumerate(lines):
            match = SNIPPET_SECTION_REGEX.match(line.strip())
            if not match:
                continue
            name = (match.group("name") or "").strip().lower()
            if name != target:
                continue
            kind = (match.group("kind") or "").strip().lower()
            if kind == "start":
                start_idx = idx + 1
            elif kind == "end" and start_idx is not None:
                return "\n".join(lines[start_idx:idx])
        if start_idx is not None:
            log.warning(
                f"[resolve_md] snippet section '{section}' missing end marker in {snippet_ref}"
            )
        else:
            log.warning(
                f"[resolve_md] snippet section '{section}' not found in {snippet_ref}"
            )
        return None

    def fetch_local_snippet(self, snippet_ref: str, snippet_directory: Path) -> str:
        """Load snippet content from docs/.snippets (optionally slicing by line range)."""
        file_only, line_start, line_end, section = self.parse_line_range(snippet_ref)
        snippet_directory_root = Path(snippet_directory).resolve()
        absolute_snippet_path = (snippet_directory_root / file_only).resolve()

        # Ensure the resolved path is still within the snippet directory to prevent traversal
        try:
            absolute_snippet_path.relative_to(snippet_directory_root)
        except ValueError:
            log.warning(f"[resolve_md] invalid local snippet path (outside snippet directory): {snippet_ref}")
            return f"<!-- INVALID LOCAL SNIPPET PATH {snippet_ref} -->"
        if not absolute_snippet_path.exists():
            log.warning(f"[resolve_md] missing local snippet {snippet_ref}")
            return f"<!-- MISSING LOCAL SNIPPET {snippet_ref} -->"

        snippet_content = absolute_snippet_path.read_text(encoding="utf-8")
        snippet_content = self.apply_snippet_selectors(
            snippet_content, line_start, line_end, section, snippet_ref
        )
        if snippet_content is None:
            return f"<!-- MISSING SNIPPET SECTION {snippet_ref} -->"
        return self.strip_snippet_section_markers(snippet_content)

    def fetch_remote_snippet(self, snippet_ref: str) -> str:
        """Retrieve remote snippet via HTTP unless remote fetching is disabled."""
        if not self.allow_remote_snippets:
            return f"<!-- REMOTE SNIPPET SKIPPED (disabled): {snippet_ref} -->"
        url, line_start, line_end, section = self.parse_line_range(snippet_ref)
        if not url.startswith("http"):
            log.warning(f"[resolve_md] invalid remote snippet ref {snippet_ref}")
            return f"<!-- INVALID REMOTE SNIPPET {snippet_ref} -->"

        try:
            with urllib_request.urlopen(url, timeout=10) as response:
                snippet_content = response.read().decode("utf-8")
        except (urllib_error.URLError, urllib_error.HTTPError) as exc:
            log.warning(
                f"[resolve_md] error fetching remote snippet {snippet_ref}: {exc}"
            )
            return f"<!-- ERROR FETCHING REMOTE SNIPPET {snippet_ref} -->"

        snippet_content = self.apply_snippet_selectors(
            snippet_content, line_start, line_end, section, snippet_ref
        )
        if snippet_content is None:
            return f"<!-- MISSING REMOTE SNIPPET SECTION {snippet_ref} -->"
        snippet_content = snippet_content.strip()
        return self.strip_snippet_section_markers(snippet_content)

    def replace_snippet_placeholders(
        self, markdown: str, snippet_directory: Path, variables: dict
    ) -> str:
        """
        Recursively replace --8<-- snippet placeholders until none remain.
        Snippet references can include {{variables}} which are resolved first.
        """

        def fetch_snippet(snippet_ref: str) -> str:
            resolved_path = self.resolve_markdown_placeholders(snippet_ref, variables)
            if resolved_path.startswith("http"):
                return self.fetch_remote_snippet(resolved_path)
            return self.fetch_local_snippet(resolved_path, snippet_directory)

        def replace_line_match(match: re.Match) -> str:
            indent = match.group("indent") or ""
            snippet_ref = match.group("ref")
            snippet_content = fetch_snippet(snippet_ref).replace("\r\n", "\n")
            if not indent:
                return snippet_content
            indented = [
                f"{indent}{line}" if line else ""
                for line in snippet_content.split("\n")
            ]
            return "\n".join(indented)

        def replace_inline_match(match: re.Match) -> str:
            snippet_ref = match.group(1)
            return fetch_snippet(snippet_ref)

        previous = None
        while previous != markdown:
            previous = markdown
            markdown = SNIPPET_LINE_REGEX.sub(replace_line_match, markdown)
            markdown = SNIPPET_TOKEN_REGEX.sub(replace_inline_match, markdown)
        return markdown

    # Remove HTML comments from Markdown

    @staticmethod
    def remove_html_comments(content: str) -> str:
        """Remove <!-- ... --> comments (multiline)."""
        return re.sub(r"<!--.*?-->", "", content, flags=re.DOTALL)

    @staticmethod
    def strip_snippet_section_markers(content: str) -> str:
        """Remove snippet section markers (# --8<-- [start:end]) from snippet content."""
        lines = content.splitlines()
        cleaned = [ln for ln in lines if not SNIPPET_SECTION_REGEX.match(ln.strip())]
        return "\n".join(cleaned)

    # Word count & token estimation

    @staticmethod
    def word_count(content: str) -> int:
        return len(re.findall(r"\b\w+\b", content, flags=re.UNICODE))

    @staticmethod
    def estimate_tokens(content: str) -> int:
        return len(re.findall(r"\w+|[^\s\w]", content, flags=re.UNICODE))

    @staticmethod
    def sha256_text(content: str) -> str:
        return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()

    def extract_outline_and_sections(
        self, body: str, max_depth: int = 3
    ) -> tuple[list[dict], list[dict]]:
        lines = body.splitlines(keepends=True)
        in_code = False
        fence = None

        starts = [0]
        for ln in lines[:-1]:
            starts.append(starts[-1] + len(ln))

        outline: list[dict] = []
        sections_meta: list[tuple[int, int, str, str]] = []
        anchors_seen: dict[str, int] = {}

        for idx, line in enumerate(lines):
            m_fence = re.match(r"^(\s*)(`{3,}|~{3,})", line)
            if m_fence:
                token = m_fence.group(2)
                if not in_code:
                    in_code, fence = True, token
                elif token == fence:
                    in_code, fence = False, None
                continue

            if in_code:
                continue

            m = HEADING_RE.match(line)
            if not m:
                continue
            hashes, text = m.group(1), m.group(2).strip()
            depth = len(hashes)
            if depth < 2 or depth > max_depth:
                continue
            anchor = self.slugify_anchor(text, anchors_seen)
            outline.append({"depth": depth, "title": text, "anchor": anchor})
            sections_meta.append((depth, idx, text, anchor))

        sections: list[dict] = []
        for idx, (depth, line_idx, title, anchor) in enumerate(sections_meta):
            start_char = starts[line_idx]
            next_start = len(body)
            if idx + 1 < len(sections_meta):
                next_start = starts[sections_meta[idx + 1][1]]
            section_text = body[start_char:next_start].strip()
            sections.append(
                {
                    "index": idx,
                    "depth": depth,
                    "title": title,
                    "anchor": anchor,
                    "start_char": start_char,
                    "end_char": next_start,
                    "text": section_text,
                }
            )

        return outline, sections

    def extract_preview(self, body: str, max_chars: int = 500) -> str:
        lines = body.splitlines()
        in_code = False
        para: list[str] = []

        def bad_start(s: str) -> bool:
            s = s.lstrip()
            return (
                not s
                or s.startswith("#")
                or s.startswith(">")
                or s.startswith("- ")
                or s.startswith("* ")
                or re.match(r"^\d+\.\s", s) is not None
            )

        def finish(buf: list[str]) -> str:
            text = " ".join(" ".join(buf).split())
            return text[:max_chars].rstrip()

        for line in lines:
            if re.match(r"^(\s*)(`{3,}|~{3,})", line):
                in_code = not in_code
                if para:
                    break
                continue
            if in_code:
                continue
            if line.strip() == "":
                if para:
                    break
                continue
            if not para and bad_start(line):
                continue
            para.append(line)

        return finish(para) if para else ""

    # Convert file path to slug, create markdown file URL

    @staticmethod
    def compute_slug_and_url(rel_path_no_ext: str, docs_base_url: str):
        """
        rel_path_no_ext: docs-relative path without extension, using OS separators.
        - If endswith '/index', drop the trailing 'index' for the URL and slug base.
        - Slug = path segments joined by '-', lowercased.
        - URL = docs_base_url + route + '/'
        """
        # Normalize to forward slashes
        route = rel_path_no_ext.replace(os.sep, "/")
        if route.endswith("/index"):
            route = route[: -len("/index")]
        # slug
        slug = route.replace("/", "-").lower()
        # url (ensure one trailing slash)
        if not route.endswith("/"):
            route = f"{route}/"
        url = f"{docs_base_url}{route}"
        return slug, url

    def get_ai_output_dir(self, base_dir: Path) -> Path:
        """Resolve target directory for resolved markdown files."""
        repo_cfg = self.llms_config.get("repository", {})
        ai_path = repo_cfg.get("ai_artifacts_path")
        if ai_path:
            ai_path = Path(ai_path)
            if not ai_path.is_absolute():
                ai_path = base_dir / ai_path
        else:
            outputs_cfg = self.llms_config.get("outputs", {})
            public_root = outputs_cfg.get("public_root", "/ai/").strip("/")
            pages_dir = outputs_cfg.get("files", {}).get("pages_dir", "pages")
            ai_path = base_dir / public_root / pages_dir

        base_dir_resolved = base_dir.resolve()
        ai_path = ai_path.resolve()
        try:
            # Ensure the resolved artifacts directory stays within the site base directory.
            ai_path.relative_to(base_dir_resolved)
        except ValueError:
            raise ValueError(
                "Configured ai_artifacts_path resolves outside of the site directory"
            ) from None

        return ai_path
    def reset_directory(self, output_dir: Path) -> None:
        """Remove existing artifacts before writing fresh files."""
        output_dir.mkdir(parents=True, exist_ok=True)
        for entry in output_dir.iterdir():
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()

    def write_ai_page(self, ai_pages_dir: Path, slug: str, header: dict, body: str):
        """Write resolved markdown with YAML front matter."""
        ai_pages_dir.mkdir(parents=True, exist_ok=True)
        out_path = ai_pages_dir / f"{slug}.md"
        fm_obj = {}
        for key in (
            "title",
            "description",
            "categories",
            "url",
            "word_count",
            "token_estimate",
        ):
            val = header.get(key)
            if val not in (None, "", []):
                fm_obj[key] = val

        fm_yaml = yaml.safe_dump(
            fm_obj, sort_keys=False, allow_unicode=True, width=4096
        ).strip()
        content = f"---\n{fm_yaml}\n---\n\n{body.strip()}\n"
        with out_path.open("w", encoding="utf-8") as fh:
            fh.write(content)
        log.debug(f"[resolve_md] wrote {out_path}")

    # Replaces copy_md plugin actions
    # Category file creation helper functions
    @staticmethod
    def slugify_category(name: str) -> str:
        s = name.strip().lower()
        s = re.sub(r"[^\w\s-]", "", s)
        s = re.sub(r"\s+", "-", s)
        s = re.sub(r"-{2,}", "-", s).strip("-")
        return s or "category"

    @staticmethod
    def slugify_anchor(text: str, seen: dict[str, int]) -> str:
        value = text.strip().lower()
        value = re.sub(r"`+", "", value)
        value = re.sub(r"[^\w\s\-]", "", value, flags=re.UNICODE)
        value = re.sub(r"\s+", "-", value)
        value = re.sub(r"-{2,}", "-", value).strip("-")
        if not value:
            value = "section"
        if value in seen:
            seen[value] += 1
            value = f"{value}-{seen[value]}"
        else:
            seen[value] = 1
        return value

    @staticmethod
    def select_pages_for_category(category: str, pages: list[dict]) -> list[dict]:
        cat_lower = category.lower()
        selected = []
        for page in pages:
            cats = page.get("categories") or []
            if isinstance(cats, str):
                cats_iter = [cats]
            else:
                cats_iter = cats
            if any(str(c).lower() == cat_lower for c in cats_iter):
                selected.append(page)
        return selected

    @staticmethod
    def union_pages(sets: list[list[dict]]) -> list[dict]:
        seen = set()
        out: list[dict] = []
        for lst in sets:
            for p in lst:
                slug = p.get("slug")
                if slug in seen:
                    continue
                seen.add(slug)
                out.append(p)
        return out

    def write_category_bundle(
        self,
        out_path: Path,
        category: str,
        includes_base: bool,
        base_categories: list[str],
        pages: list[dict],
        resolved_base: str,
    ) -> None:
        """Concatenate pages into a single Markdown bundle."""
        out_path.parent.mkdir(parents=True, exist_ok=True)
        total_words = sum(p.get("word_count", 0) for p in pages)
        total_tokens = sum(p.get("token_estimate", 0) for p in pages)

        fm_obj = {
            "category": category,
            "includes_base_categories": bool(includes_base),
            "base_categories": base_categories if includes_base else [],
            "word_count": total_words,
            "token_estimate": total_tokens,
            "page_count": len(pages),
        }
        fm_yaml = yaml.safe_dump(
            fm_obj, sort_keys=False, allow_unicode=True, width=4096
        ).strip()

        lines: list[str] = [f"---\n{fm_yaml}\n---\n"]
        lines.append(f"# Begin New Bundle: {category}")
        if includes_base and base_categories:
            lines.append(
                f"Includes shared base categories: {', '.join(base_categories)}"
            )
        lines.append("")

        for page in pages:
            lines.append("\n---\n")
            title = page.get("title") or page["slug"]
            lines.append(f"Page Title: {title}\n")
            resolved_url = f"{resolved_base}/{page['slug']}.md" if resolved_base else f"{page['slug']}.md"
            lines.append(f"- Resolved Markdown: {resolved_url}")
            html_url = page.get("url")
            if html_url:
                lines.append(f"- Canonical (HTML): {html_url}")
            description = page.get("description")
            if description:
                lines.append(f"- Summary: {description}")
            lines.append(
                f"- Word Count: {page.get('word_count', 0)}; Token Estimate: {page.get('token_estimate', 0)}"
            )
            lines.append("")
            lines.append(page.get("body", "").strip())
            lines.append("")

        out_path.write_text("\n".join(lines), encoding="utf-8")

    def build_category_bundles(self, pages: list[dict], ai_root: Path) -> None:
        """Generate per-category bundle files based on AI pages."""
        content_cfg = self.llms_config.get("content", {})
        categories_order = content_cfg.get("categories_order") or []
        if not categories_order:
            log.info("[resolve_md] no categories configured; skipping bundles")
            return
        base_cats = content_cfg.get("base_context_categories") or []

        categories_dir = ai_root / "categories"
        self.reset_directory(categories_dir)

        resolved_base = self.build_resolved_base_url()

        base_sets = [self.select_pages_for_category(cat, pages) for cat in base_cats]
        base_union = self.union_pages(base_sets) if base_sets else []

        log.debug(
            f"[resolve_md] building category bundles for {len(categories_order)} categories; sample page cats: {pages[0].get('categories') if pages else 'none'}"
        )

        for category in categories_order:
            cat_slug = self.slugify_category(category)
            out_path = categories_dir / f"{cat_slug}.md"
            is_base = category in base_cats
            category_pages = self.select_pages_for_category(category, pages)

            if is_base:
                bundle_pages = sorted(
                    category_pages, key=lambda p: p.get("title", "").lower()
                )
                log.debug(
                    f"[resolve_md] base bundle {category}: {len(bundle_pages)} pages"
                )
                self.write_category_bundle(
                    out_path, category, False, base_cats, bundle_pages, resolved_base
                )
            else:
                combined = self.union_pages([base_union, category_pages])
                bundle_pages = sorted(
                    combined, key=lambda p: p.get("title", "").lower()
                )
                log.debug(
                    f"[resolve_md] category bundle {category}: base={len(base_union)} cat-only={len(category_pages)} total={len(bundle_pages)}"
                )
                self.write_category_bundle(
                    out_path, category, True, base_cats, bundle_pages, resolved_base
                )

        log.info(f"[resolve_md] category bundles written to {categories_dir}")

    # Create full-site content related AI artifact files
    def build_site_index(self, pages: list[dict], ai_root: Path) -> None:
        """Generate site-index.json and llms_full.jsonl from AI pages."""
        if not pages:
            return
        outputs = self.llms_config.get("outputs", {})
        files_cfg = outputs.get("files", {})
        site_index_name = files_cfg.get("site_index", "site-index.json")
        llms_full_name = files_cfg.get("llms_full", "llms-full.jsonl")
        preview_chars = outputs.get("preview_chars", 500)
        max_depth = outputs.get("outline_max_depth", 3)
        token_estimator = "heuristic-v1"

        index_path = ai_root / site_index_name
        llms_path = ai_root / llms_full_name
        index_path.parent.mkdir(parents=True, exist_ok=True)
        llms_path.parent.mkdir(parents=True, exist_ok=True)

        resolved_base = self.build_resolved_base_url()
        site_index: list[dict] = []
        jsonl_lines: list[str] = []

        for page in pages:
            body = page.get("body", "")
            outline, sections = self.extract_outline_and_sections(
                body, max_depth=max_depth
            )
            preview = self.extract_preview(body, max_chars=preview_chars) or page.get(
                "description", ""
            )
            total_section_tokens = 0
            for sec in sections:
                sec_tokens = self.estimate_tokens(sec["text"])
                total_section_tokens += sec_tokens
                jsonl_lines.append(
                    json.dumps(
                        {
                            "page_id": page["slug"],
                            "page_title": page.get("title"),
                            "index": sec["index"],
                            "depth": sec["depth"],
                            "title": sec["title"],
                            "anchor": sec["anchor"],
                            "start_char": sec["start_char"],
                            "end_char": sec["end_char"],
                            "estimated_token_count": sec_tokens,
                            "token_estimator": token_estimator,
                            "text": sec["text"],
                        },
                        ensure_ascii=False,
                    )
                )

            stats = {
                "word_count": page.get("word_count", 0),
                "token_estimate": page.get("token_estimate", total_section_tokens),
                "headings": len(outline),
                "sections_indexed": len(sections),
            }

            resolved_md_url = (
                f"{resolved_base}/{page['slug']}.md" if resolved_base else f"{page['slug']}.md"
            )
            entry = {
                "id": page["slug"],
                "title": page.get("title"),
                "slug": page["slug"],
                "categories": page.get("categories", []),
                "resolved_md_url": resolved_md_url,
                "html_url": page.get("url"),
                "preview": preview,
                "outline": outline,
                "stats": stats,
                "hash": self.sha256_text(body),
                "token_estimator": token_estimator,
            }
            entry["raw_md_url"] = resolved_md_url
            site_index.append(entry)

        index_path.write_text(
            json.dumps(site_index, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        with llms_path.open("w", encoding="utf-8") as fh:
            for line in jsonl_lines:
                fh.write(line + "\n")

        log.info(
            f"[resolve_md] site index written to {index_path} (pages={len(site_index)})"
        )
        log.info(
            f"[resolve_md] llms full JSONL written to {llms_path} (sections={len(jsonl_lines)})"
        )

    def build_llms_txt(self, pages: list[dict], docs_dir: Path) -> None:
        """Generate llms.txt listing resolved markdown links grouped by category."""
        if not pages:
            return
        resolved_base = self.build_resolved_base_url()

        project_cfg = self.llms_config.get("project", {})
        project_name = project_cfg.get("name", "Documentation")
        summary_line = project_cfg.get("project_url") or project_cfg.get(
            "docs_base_url", ""
        )

        content_cfg = self.llms_config.get("content", {})
        category_order = content_cfg.get("categories_order", []) or []

        docs_root = docs_dir.resolve()
        output_rel = self.llms_config.get("llms_txt_output_path", "llms.txt")
        out_path = Path(output_rel)
        if not out_path.is_absolute():
            out_path = (docs_root / out_path).resolve()
        else:
            out_path = out_path.resolve()

        try:
            out_path.relative_to(docs_root)
        except ValueError as exc:
            raise ValueError(
                f"Configured llms_txt_output_path '{output_rel}' must be within docs_dir '{docs_root}'"
            ) from exc
        out_path.parent.mkdir(parents=True, exist_ok=True)

        metadata_section = self.format_llms_metadata_section(pages)
        docs_section = self.format_llms_docs_section(
            pages, resolved_base, category_order
        )
        summary_line = summary_line.strip()

        content_lines = [
            f"# {project_name}",
            f"\n> {summary_line}\n" if summary_line else "",
            "## How to Use This File",
            (
                "This file lists URLs for resolved Markdown pages that complement the rendered pages on the documentation site. "
                "Use these Markdown files when prompting models to retain semantic context without HTML noise."
            ),
            "",
            metadata_section,
            docs_section,
        ]

        out_path.write_text(
            "\n".join(line for line in content_lines if line is not None),
            encoding="utf-8",
        )
        log.info(f"[resolve_md] llms.txt written to {out_path}")

    @staticmethod
    def format_llms_metadata_section(pages: list[dict]) -> str:
        distinct_categories = {
            cat for page in pages for cat in (page.get("categories") or [])
        }
        return "\n".join(
            [
                "## Metadata",
                f"- Documentation pages: {len(pages)}",
                f"- Categories: {len(distinct_categories)}",
                "",
            ]
        )

    @staticmethod
    def format_llms_docs_section(
        pages: list[dict], resolved_base: str, category_order: list[str]
    ) -> str:
        grouped: dict[str, list[str]] = {}
        for page in pages:
            resolved_url = (
                f"{resolved_base}/{page['slug']}.md"
                if resolved_base
                else f"{page['slug']}.md"
            )
            title = page.get("title") or page["slug"]
            description = page.get("description") or ""
            cats = page.get("categories") or ["Uncategorized"]
            line = f"- [{title}]({resolved_url}): {description}"
            for cat in cats:
                grouped.setdefault(cat, []).append(line)

        lines = [
            "## Docs",
            "This section lists documentation pages by category. Each entry links to the resolved markdown version of the page and includes a short description.",
        ]
        seen = set()
        for cat in category_order:
            entries = grouped.get(cat)
            if not entries:
                continue
            lines.append(f"\nDocs: {cat}")
            lines.extend(entries)
            seen.add(cat)

        remaining = sorted(cat for cat in grouped if cat not in seen)
        for cat in remaining:
            lines.append(f"\nDocs: {cat}")
            lines.extend(grouped[cat])

        return "\n".join(lines)

    def get_resolved_pages_relpath(self) -> str:
        """Return the site-relative path where resolved markdown files are published."""
        repo_cfg = self.llms_config.get("repository", {})
        ai_path = repo_cfg.get("ai_artifacts_path")
        if ai_path:
            ai_str = str(ai_path)
            if ai_str and not os.path.isabs(ai_str):
                rel_path = ai_str.strip("/")
                if rel_path:
                    return rel_path
        outputs = self.llms_config.get("outputs", {})
        public_root = outputs.get("public_root", "/ai/").strip("/")
        pages_dir = outputs.get("files", {}).get("pages_dir", "pages").strip("/")
        rel_path = "/".join(part for part in (public_root, pages_dir) if part)
        return rel_path or "ai/pages"

    def build_resolved_base_url(self) -> str:
        """Return base URL for resolved markdown artifacts on the published site."""
        rel_path = self.get_resolved_pages_relpath()
        base = self.docs_base_url or "/"
        if rel_path:
            prefix = f"{base}{rel_path}" if base.endswith("/") else f"{base}/{rel_path}"
        else:
            prefix = base
        cleaned = prefix.rstrip("/")
        return cleaned or "/"
