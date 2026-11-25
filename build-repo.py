#!/usr/bin/env python3
"""Build a flat Cydia/APT index from every Debian package under debs/."""
import gzip
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile

ROOT = os.path.dirname(os.path.abspath(__file__))
DEBS = os.path.join(ROOT, "debs")
GENERATED_FIELDS = {"filename", "size", "md5sum", "sha256"}


def parse_control(text):
    fields = {}
    current = None
    for line in text.splitlines():
        if line.startswith((" ", "\t")) and current:
            fields[current] += "\n" + line
        elif ":" in line:
            current, value = line.split(":", 1)
            fields[current] = value.strip()
        elif not line:
            current = None
    return fields


def read_control_without_dpkg(path):
    with open(path, "rb") as package:
        data = package.read()
    if not data.startswith(b"!<arch>\n"):
        raise ValueError("not a Debian ar archive: " + path)

    cursor = 8
    while cursor + 60 <= len(data):
        header = data[cursor:cursor + 60]
        name = header[:16].decode("ascii").strip().rstrip("/")
        size = int(header[48:58].decode("ascii").strip())
        body_start = cursor + 60
        body = data[body_start:body_start + size]
        if len(body) != size:
            raise ValueError("truncated Debian ar member in " + path)
        cursor = body_start + size + (size & 1)

        if not name.startswith("control.tar"):
            continue
        if name.endswith(".gz"):
            mode = "r:gz"
        elif name.endswith(".xz"):
            mode = "r:xz"
        elif name.endswith(".bz2"):
            mode = "r:bz2"
        elif name.endswith(".zst"):
            raise ValueError(
                "control.tar.zst requires dpkg-deb; install dpkg and rerun this script"
            )
        else:
            mode = "r:"
        with tarfile.open(fileobj=io.BytesIO(body), mode=mode) as archive:
            for member in archive.getmembers():
                if member.name.lstrip("./") == "control":
                    control = archive.extractfile(member)
                    if control is None:
                        break
                    return control.read().decode("utf-8")
    raise ValueError("no readable control file in " + path)


def read_control(path):
    dpkg_deb = shutil.which("dpkg-deb")
    if dpkg_deb:
        result = subprocess.run(
            [dpkg_deb, "-f", path],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return result.stdout
    return read_control_without_dpkg(path)


def main():
    if not os.path.isdir(DEBS):
        os.makedirs(DEBS)

    package_paths = []
    for directory, _, filenames in os.walk(DEBS):
        for filename in filenames:
            if filename.endswith(".deb"):
                package_paths.append(os.path.join(directory, filename))
    package_paths.sort()

    entries = []
    identities = set()
    architectures = set()
    for path in package_paths:
        fields = parse_control(read_control(path))
        for required in ("Package", "Version", "Architecture"):
            if not fields.get(required):
                raise ValueError("%s is missing %s metadata" % (path, required))

        identity = (fields["Package"], fields["Version"], fields["Architecture"])
        if identity in identities:
            raise ValueError("duplicate package/version/architecture: %s" % (identity,))
        identities.add(identity)

        relative = os.path.relpath(path, ROOT).replace(os.sep, "/")
        if any(character.isspace() for character in relative):
            raise ValueError("package file path cannot contain whitespace: " + relative)

        with open(path, "rb") as package:
            blob = package.read()
        output_fields = {
            key: value
            for key, value in fields.items()
            if key.lower() not in GENERATED_FIELDS
        }
        output_fields["Filename"] = relative
        output_fields["Size"] = str(len(blob))
        output_fields["MD5sum"] = hashlib.md5(blob).hexdigest()
        output_fields["SHA256"] = hashlib.sha256(blob).hexdigest()

        stanza = "".join("%s: %s\n" % (key, value) for key, value in output_fields.items())
        entries.append(stanza + "\n")
        architecture = fields["Architecture"]
        if architecture and architecture != "all":
            architectures.add(architecture)

    index = "".join(entries).encode("utf-8")
    compressed = gzip.compress(index, mtime=0)
    with open(os.path.join(ROOT, "Packages"), "wb") as output:
        output.write(index)
    with open(os.path.join(ROOT, "Packages.gz"), "wb") as output:
        output.write(compressed)

    if not architectures:
        architectures.update(("iphoneos-arm", "iphoneos-arm64"))
    release_lines = [
        "Origin: Andrija Cydia",
        "Label: Cydia",
        "Suite: stable",
        "Version: 1.0",
        "Codename: stable",
        "Architectures: " + " ".join(sorted(architectures)),
        "Components: main",
        "Description: Personal repository for jailbreak packages",
    ]
    checksums = {
        "Packages": index,
        "Packages.gz": compressed,
    }
    for algorithm in ("MD5", "SHA256"):
        release_lines.extend(("", algorithm + "Sum:"))
        digest_function = hashlib.md5 if algorithm == "MD5" else hashlib.sha256
        for filename, content in checksums.items():
            release_lines.append(
                " %s %d %s"
                % (digest_function(content).hexdigest(), len(content), filename)
            )
    with open(os.path.join(ROOT, "Release"), "w", encoding="utf-8") as output:
        output.write("\n".join(release_lines) + "\n")

    print("Indexed %d package version(s)." % len(entries))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
