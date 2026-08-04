# Configuration file for the Sphinx documentation builder.
#
# For the full list of built-in configuration values, see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

# -- Project information -----------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#project-information

import os
import sys

sys.path.insert(0, os.path.abspath(".."))

project = "hoplink"
copyright = "2026, MoonShardFlower"
author = "MoonShardFlower"
release = "0.1.0"

# -- General configuration ---------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#general-configuration

extensions = [
    "sphinx.ext.todo",
    "sphinx.ext.viewcode",
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
]

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

# Render docstring "Attributes:" sections as inline :ivar: fields instead of separate
# attribute objects, so they don't collide with the dataclass fields autodoc already
# documents (which otherwise produces "duplicate object description" warnings).
napoleon_use_ivar = True

# The top-level package re-exports the public API (see hoplink.__all__), so a class like Events is reachable as
# both hoplink.Events and hoplink.events.Events. That makes bare cross-references ambiguous. Silence
# those, rather than dropping the convenience re-exports.
suppress_warnings = ["ref.python"]

# -- Options for HTML output -------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#options-for-html-output

html_theme = "sphinx_rtd_theme"
