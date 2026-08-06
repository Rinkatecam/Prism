"""Operator CLI tools for Prism (backup, restore, etc.).

These are intentionally standalone scripts that depend only on the Python
stdlib (plus pywin32, lazily) so an operator can run them on a host where
the full Prism virtualenv is not yet bootstrapped.
"""
