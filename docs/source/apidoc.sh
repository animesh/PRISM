export SPHINX_APIDOC_OPTIONS=members
sphinx-apidoc --ext-autodoc -P -E -e -T -M -f -d 2 --implicit-namespaces -o source/api/ ../prism/ ../prism/*/tests/* ../prism/tests/* ../prism/__version__.py ../prism/_docstrings.py