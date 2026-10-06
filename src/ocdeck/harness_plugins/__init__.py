"""In-tree harness plugins.

Every module here (except names starting with ``_``) is imported when OC Deck
starts and may call ``ocdeck.harnesses.register_harness``. Copy ``_template.py``
to ``<harness>.py`` to add a harness; no other file needs editing.
"""
