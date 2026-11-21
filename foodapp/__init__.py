"""Food app package. Importing it makes HTTPS work on Python builds without a certificate bundle (see netutil)."""

from foodapp.netutil import install_ca_fallback

install_ca_fallback()
