"""The member-plane agent runtime (v4).

One process = one member = one uid. This package is what
`pai@<member>.service` ExecStarts as the member's own user; it serves
exactly that member and knows nothing of the fleet. It is installed
root-owned under /usr/lib/pai/ — members execute it, never write it.

See usr/share/doc/FILESYSTEM_v4.md.
"""
