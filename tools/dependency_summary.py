#
# This script can be used to generate a summary of our third-party dependencies,
# including license details. Use it like this:
#
#    $> python dependency_summary.py --package <package name>
#
# It shells out to `cargo metadata` to gather information about the full dependency tree
# and to `cargo build --build-plan` to figure out the dependencies of the specific target package.
#
# N.B. to generate dependencies for iOS build targets, you have to run this on a Mac,
# otherwise the necessary targets are simply not available in cargo.
#
# XXX TODO: include optional notice for SQLite and zlib (via adler32)
# XXX TODO: update dependency-management docs to mention this script
# XXX TODO: Apache license makes special mention of handling of a "NOTICE" text file included
#           in the distribution, we should check for this explicitly.

import io
import re
import sys
import os.path
import argparse
import subprocess
import hashlib
import json
import itertools
import collections
import requests

# The targets used by rust-android-gradle, excluding the ones for unit testing.
# https://github.com/mozilla/rust-android-gradle/blob/master/plugin/src/main/kotlin/com/nishtahir/RustAndroidPlugin.kt
ALL_ANDROID_TARGETS = [
    "armv7-linux-androideabi",
    "aarch64-linux-android",
    "i686-linux-android",
    "x86_64-linux-android",
]

# The targets used when compiling for iOS.
# From ../build-scripts/xc-universal-binary.sh
ALL_IOS_TARGETS = [
    "x86_64-apple-ios",
    "aarch64-apple-ios"
]

# The licenses under which we can compatibly use dependencies,
# in the order in which we prefer them.
LICENES_IN_PREFERENCE_ORDER = [
    # MPL is our own license and is therefore clearly the best :-)
    "MPL-2.0",
    # We like Apache2.0 because of its patent grant clauses, and its
    # easily-dedupable license text that doesn't get customized per project.
    "Apache-2.0",
    # The MIT license is pretty good, because it's short.
    "MIT",
    # Creative Commons Zero is the only Creative Commons license that's MPL-comaptible.
    "CC0-1.0",
    # BSD and similar licenses are pretty good; the fewer clauses the better.
    "ISC",
    "BSD-2-Clause",
    "BSD-3-Clause",
]


# Packages that get pulled into our dependency tree but we know we definitely don't
# ever build with in practice, typically because they're platform-specific support
# for platforms we don't actually support.
EXCLUDED_PACKAGES = set([
    "fuchsia-cprng",
    "fuchsia-zircon",
    "fuchsia-zircon-sys",
])

# Known metadata for special extra packages that are not managed by cargo.
EXTRA_PACKAGE_METADATA = {
    "ext-jna" : {
        "name": "jna",
        "repository": "https://github.com/java-native-access/jna",
        "license": "Apache-2.0",
        "license_file": "https://raw.githubusercontent.com/java-native-access/jna/master/AL2.0",
    },
    "ext-protobuf": {
        "name": "protobuf",
        "repository": "https://github.com/protocolbuffers/protobuf",
        "license": "BSD-3-Clause",
        "license_file": "https://raw.githubusercontent.com/protocolbuffers/protobuf/master/LICENSE",
    },
    "ext-swift-protobuf": {
        "name": "swift-protobuf",
        "repository": "https://github.com/apple/swift-protobuf",
        "license": "Apache-2.0",
        "license_file": "https://raw.githubusercontent.com/apple/swift-protobuf/master/LICENSE.txt"
    },
    "ext-openssl": {
        "name": "openssl",
        "repository": "https://www.openssl.org/source/",
        "license": "OpenSSL",
        "license_file": "https://www.openssl.org/source/license-openssl-ssleay.txt",
    },
    "ext-sqlcipher": {
        "name": "sqlcipher",
        "repository": "https://github.com/sqlcipher/sqlcipher",
        "license": "BSD-3-Clause",
        "license_file": "https://raw.githubusercontent.com/sqlcipher/sqlcipher/master/LICENSE",
    },
}

# And these are rust packages that pull in the above dependencies.
# Others are added on a per-target basis during dependency resolution.
PACKAGES_WITH_EXTRA_DEPENDENCIES = {
    "openssl-sys": ["ext-openssl"],
    "ring": ["ext-openssl"],
    # As a special case, we know that the "logins" crate is the only thing that enables SQLCipher.
    # In a future iteration we could check the cargo build-plan output to see whether anything is
    # enabling the sqlcipher feature, but this will do for now.
    "logins": ["ext-sqlcipher"],
}

# Hand-audited tweaks to package metadata, for cases where the data given to us by cargo is insufficient.
# We list both the expected value from `cargo metadata` and the replacement value, to
# guard against accidentally overwriting future upstream changes in the metadata.
#
# Let's try not to add any more dependencies that require us to edit this list!
PACKAGE_METADATA_FIXUPS = {
    # Ring's license describes itself as "ISC-like", and we've reviewed this manually.
    "ring": {
        "license": {
            "check": None,
            "fixup": "ISC",
        },
    },
    # In this case the rust code is BSD-3-Clause and the wrapped zlib library is under the Zlib license,
    # which does not require explicit attribution.
    "adler32": {
        "license": {
            "check": "BSD-3-Clause AND Zlib",
            "fixup": "BSD-3-Clause",
        },
        "license_file": {
            "check": None,
            "fixup": "LICENSE",
        }
    },
    # These packages do not unambiguously delcare their licensing file.
    "publicsuffix": {
        "license": {
            "check": "MIT/Apache-2.0"
        },
        "license_file": {
            "check": None,
            "fixup": "LICENSE-APACHE",
        }
    },
    "siphasher": {
        "license": {
            "check": "MIT/Apache-2.0"
        },
        "license_file": {
            "check": None,
            "fixup": "COPYING",
        }
    },
    # These packages do not include their license file in their release distributions,
    # so we have to fetch it over the network. XXX TODO: upstream bugs to get it included?
    "argon2rs": {
        "repository": {
            "check": "https://github.com/bryant/argon2rs",
        },
        "license_file": {
            "check": None,
            "fixup": "https://raw.githubusercontent.com/bryant/argon2rs/master/LICENSE",
        }
    },
    "cloudabi": {
        "repository": {
            "check": "https://github.com/nuxinl/cloudabi",
        },
        "license_file": {
            "check": None,
            "fixup": "https://raw.githubusercontent.com/nuxinl/cloudabi/master/LICENSE",
        }
    },
    "failure_derive": {
        "repository": {
            "check": "https://github.com/withoutboats/failure_derive",
        },
        "license_file": {
            "check": None,
            "fixup": "https://raw.githubusercontent.com/withoutboats/failure_derive/master/LICENSE-APACHE",
        }
    },
    "hawk": {
        "repository": {
            "check": "https://github.com/taskcluster/rust-hawk",
        },
        "license_file": {
            "check": None,
            "fixup": "https://raw.githubusercontent.com/taskcluster/rust-hawk/master/LICENSE",
        }
    },
    "kernel32-sys": {
        "repository": {
            "check": "https://github.com/retep998/winapi-rs",
        },
        "license_file": {
            "check": None,
            "fixup": "https://raw.githubusercontent.com/retep998/winapi-rs/master/LICENSE-APACHE",
        }
    },
    "libsqlite3-sys": {
        "repository": {
            "check": "https://github.com/jgallagher/rusqlite",
        },
        "license_file": {
            "check": None,
            "fixup": "https://raw.githubusercontent.com/jgallagher/rusqlite/master/LICENSE",
        }
    },
    "mockiato-codegen": {
        "repository": {
            "check": "https://github.com/myelin-ai/mockiato",
        },
        "license_file": {
            "check": None,
            "fixup": "https://raw.githubusercontent.com/myelin-ai/mockiato/master/license.txt",
        }
    },
    "phf": {
        "repository": {
            "check": "https://github.com/sfackler/rust-phf",
        },
        "license_file": {
            "check": None,
            "fixup": "https://raw.githubusercontent.com/sfackler/rust-phf/master/LICENSE",
        }
    },
    "phf_codegen": {
        "repository": {
            "check": "https://github.com/sfackler/rust-phf",
        },
        "license_file": {
            "check": None,
            "fixup": "https://raw.githubusercontent.com/sfackler/rust-phf/master/LICENSE",
        }
    },
    "phf_generator": {
        "repository": {
            "check": "https://github.com/sfackler/rust-phf",
        },
        "license_file": {
            "check": None,
            "fixup": "https://raw.githubusercontent.com/sfackler/rust-phf/master/LICENSE",
        },
    },
    "phf_shared": {
        "repository": {
            "check": "https://github.com/sfackler/rust-phf",
        },
        "license_file": {
            "check": None,
            "fixup": "https://raw.githubusercontent.com/sfackler/rust-phf/master/LICENSE",
        },
    },
    "prost-build": {
        "repository": {
            "check": "https://github.com/danburkert/prost",
        },
        "license_file": {
            "check": None,
            "fixup": "https://raw.githubusercontent.com/danburkert/prost/master/LICENSE",
        },
    },
    "prost-derive": {
        "repository": {
            "check": "https://github.com/danburkert/prost",
        },
        "license_file": {
            "check": None,
            "fixup": "https://raw.githubusercontent.com/danburkert/prost/master/LICENSE",
        },
    },
    "prost-types": {
        "repository": {
            "check": "https://github.com/danburkert/prost",
        },
        "license_file": {
            "check": None,
            "fixup": "https://raw.githubusercontent.com/danburkert/prost/master/LICENSE",
        },
    },
    "rgb": {
        "repository": {
            "check": "https://github.com/kornelski/rust-rgb",
        },
        "license_file": {
            "check": None,
            "fixup": "https://raw.githubusercontent.com/kornelski/rust-rgb/master/LICENSE",
        },
    },
    "security-framework": {
        "repository": {
            "check": "https://github.com/kornelski/rust-security-framework",
        },
        "license_file": {
            "check": None,
            "fixup": "https://raw.githubusercontent.com/kornelski/rust-security-framework/master/LICENSE-APACHE",
        },
    },
    "security-framework-sys": {
        "repository": {
            "check": "https://github.com/kornelski/rust-security-framework",
        },
        "license_file": {
            "check": None,
            "fixup": "https://raw.githubusercontent.com/kornelski/rust-security-framework/master/LICENSE-APACHE",
        },
    },
    "url_serde": {
        "repository": {
            "check": "https://github.com/servo/rust-url",
        },
        "license_file": {
            "check": None,
            "fixup": "https://raw.githubusercontent.com/servo/rust-url/master/LICENSE-APACHE",
        },
    },
    "vcpkg": {
        "repository": {
            "check": "https://github.com/mcgoo/vcpkg-rs",
        },
        "license_file": {
            "check": None,
            "fixup": "https://raw.githubusercontent.com/mcgoo/vcpkg-rs/master/LICENSE-APACHE",
        },
    },
    "void": {
        "repository": {
            "check": "https://github.com/reem/rust-void.git",
        },
        "license_file": {
            "check": None,
            "fixup": "https://raw.githubusercontent.com/reem/rust-void/master/LICENSE-APACHE",
        },
    },
    "winapi-build": {
        "repository": {
            "check": "https://github.com/retep998/winapi-rs",
        },
        "license_file": {
            "check": None,
            "fixup": "https://raw.githubusercontent.com/retep998/winapi-rs/master/LICENSE-APACHE",
        },
    },
    "winapi-i686-pc-windows-gnu": {
        "repository": {
            "check": "https://github.com/retep998/winapi-rs",
        },
        "license_file": {
            "check": None,
            "fixup": "https://raw.githubusercontent.com/retep998/winapi-rs/master/LICENSE-APACHE",
        },
    },
    "winapi-x86_64-pc-windows-gnu": {
        "repository": {
            "check": "https://github.com/retep998/winapi-rs",
        },
        "license_file": {
            "check": None,
            "fixup": "https://raw.githubusercontent.com/retep998/winapi-rs/master/LICENSE-APACHE",
        },
    },
    "ws2_32-sys": {
        "repository": {
            "check": "https://github.com/retep998/winapi-rs",
        },
        "license_file": {
            "check": None,
            "fixup": "https://raw.githubusercontent.com/retep998/winapi-rs/master/LICENSE-APACHE",
        },
    },
    # These packages don't have an explicit license file, we can only take the SPDX license
    # declaration from their Cargo.toml at its word. XXX TODO: is that OK?
    "clicolors-control": {
        "license": {
            "check": "MIT",
        },
        "license_text": {
            "check": None,
            "fixup": "No license text provided",
        },
    },
    "constant_time_eq": {
        "license": {
            "check": "CC0-1.0",
        },
        "license_text": {
            "check": None,
            "fixup": "No license text provided",
        },
    },
    "ctor": {
        "license": {
            "check": "Apache-2.0 OR MIT",
            "fixup": "Apache-2.0",
        },
        "license_text": {
            "check": None,
            "fixup": "No license text provided",
        },
    },
    "find-places-db": {
        "license": {
            "check": "MIT/Apache-2.0",
            "fixup": "Apache-2.0",
        },
        "license_text": {
            "check": None,
            "fixup": "No license text provided",
        },
    },
    "more-asserts": {
        "license": {
            "check": "CC0-1.0",
        },
        "license_text": {
            "check": None,
            "fixup": "No license text provided",
        },
    },
}

# Sets of common licence file names, by license type.
# If we can find one and only one of these files in a package, then we can be confident
# that it's the intended license text.
COMMON_LICENSE_FILE_NAME_ROOTS = {
    "": ["license", "licence"],
    "Apache-2.0": ["license-apache", "licence-apache"],
    "MIT": ["license-mit", "licence-mit"],
}
COMMON_LICENSE_FILE_NAME_SUFFIXES = ["", ".md", ".txt"]
COMMON_LICENSE_FILE_NAMES = {}
for license in COMMON_LICENSE_FILE_NAME_ROOTS:
    COMMON_LICENSE_FILE_NAMES[license] = set()
    for suffix in COMMON_LICENSE_FILE_NAME_SUFFIXES:
        for root in COMMON_LICENSE_FILE_NAME_ROOTS[license]:
            COMMON_LICENSE_FILE_NAMES[license].add(root + suffix)
        for root in COMMON_LICENSE_FILE_NAME_ROOTS[""]:
            COMMON_LICENSE_FILE_NAMES[license].add(root + suffix)


def execute_for_each_target(cmd, targets):
    """Execute a cargo command for each of the given targets, returning stdout.

    This function takes a tuple representing a cargo command line, executes it once for each
    `--target` or `--all-targets` option implied by the given list of targets, and returns
    an iterator over the stdout of each execution.
    """
    extraArgs = []
    if not targets:
        extraArgs.append(('--all-targets',))
    else:
        for target in iter_targets(targets):
            extraArgs.append(('--target', target,))
    for args in extraArgs:
        p = subprocess.run(cmd + args, stdout=subprocess.PIPE, universal_newlines=True)
        p.check_returncode()
        yield p.stdout


def iter_targets(targets):
    if targets:
        if isinstance(targets, str):
            yield targets
        else:
            for target in targets:
                yield target


def targets_include_android(targets):
    """Determine whether the given build targets include any android platforms."""
    if not targets:
        return True
    for target in iter_targets(targets):
        if target.endswith("-android") or target.endswith("-androideabi"):
            return True
    return False


def targets_include_ios(targets):
    """Determine whether the given build targets include any android platforms."""
    if not targets:
        return True
    for target in iter_targets(targets):
        if target.endswith("-ios"):
            return True
    return False


def get_workspace_metadata():
    """Get metadata for all dependencies in the workspace."""
    p = subprocess.run([
        'cargo', '+nightly', 'metadata', '--locked', '--format-version', '1'
    ], stdout=subprocess.PIPE, universal_newlines=True)
    p.check_returncode()
    return WorkspaceMetadata(json.loads(p.stdout))


def print_dependency_summary(deps, file=sys.stdout):
    """Print a nicely-formatted summary of dependencies and their license info."""
    def pf(string, *args):
        if args:
            string = string.format(*args)
        print(string, file=file)

    # Dedupe by shared license text where possible.
    depsByLicenseTextHash = collections.defaultdict(list)
    for info in deps:
        if info["license"] in ("MPL-2.0", "Apache-2.0", "OpenSSL"):
            # We know these licenses to have shared license text, sometimes differing on e.g. punctuation details.
            # XXX TODO: should check this more explicitly to ensure they contain the expected text.
            licenseTextHash = info["license"]
        else:
            # Other license texts typically include copyright notices that we can't dedupe, except on whitespace.
            text = "".join(info["license_text"].split())
            licenseTextHash = info["license"] + ":" + hashlib.sha256(text.encode("utf8")).hexdigest()
        depsByLicenseTextHash[licenseTextHash].append(info)

    # List licenses in the order in which we prefer them, then in alphabetical order
    # of the dependency names. This ensures a convenient and stable ordering.
    def sort_key(licenseTextHash):
        for i, license in enumerate(LICENES_IN_PREFERENCE_ORDER):
            if licenseTextHash.startswith(license):
                return (i, sorted(info["name"] for info in depsByLicenseTextHash[licenseTextHash]))
        return (i + 1, sorted(info["name"] for info in depsByLicenseTextHash[licenseTextHash]))

    sections = sorted(depsByLicenseTextHash.keys(), key=sort_key)

    pf("# Licenses for Third-Party Dependencies")
    pf("")
    pf("Software packages built from this source code may incorporate code from a number of third-party dependencies.")
    pf("These dependencies are available under a variety of free and open source licenses,")
    pf("the details of which are reproduced below.")
    pf("")

    # First a "table of contents" style thing.
    for licenseTextHash in sections:
        header = format_license_header(licenseTextHash, depsByLicenseTextHash[licenseTextHash])
        pf("* [{}](#{})", header, header_to_anchor(header))

    pf("-------------")

    # Now the actual license details.
    for licenseTextHash in sections:
        deps = sorted(depsByLicenseTextHash[licenseTextHash], key=lambda i: i["name"])
        licenseText = deps[0]["license_text"]
        for dep in deps:
            licenseText = dep["license_text"]
            # As a bit of a hack, we need to find a copy of the apache license text
            # that still has the copyright placeholders in it.
            if licenseTextHash != "Apache-2.0" or "[yyyy]" in licenseText:
                break
        else:
            raise RuntimeError("Could not find appropriate apache license text")
        pf("## {}", format_license_header(licenseTextHash, deps))
        pf("")
        pkgs = ["[{}]({})".format(info["name"], info["repository"]) for info in deps]
        pkgs = sorted(set(pkgs)) # Dedupe in case of multiple versons of dependencies.
        pf("This license applies to code linked from the following dependendencies: {}", ", ".join(pkgs))
        pf("")
        pf("```")
        assert "```" not in licenseText
        pf("{}", licenseText)
        pf("```")
        pf("-------------")


def format_license_header(license, deps):
    if license == "MPL-2.0":
        return "Mozilla Public License 2.0"
    if license == "Apache-2.0":
        return "Apache License 2.0"
    if license == "OpenSSL":
        return "OpenSSL License"
    license = license.split(":")[0]
    # Dedupe in case of multiple versons of dependencies
    names=sorted(set(info["name"] for info in deps))
    return "{} License: {}".format(license, ", ".join(names))


def header_to_anchor(header):
    return header.lower().replace(" ", "-").replace(".", "").replace(",", "").replace(":", "")


class WorkspaceMetadata(object):
    """Package metadata for all dependencies in the workspace.

    This uses `cargo metadata` to load the complete set of package metadata for the dependency tree
    of our workspace.  It does a union of all features required by all packages in the workspace,
    being a strict superset of them.
    
    For the JSON data format, ref https://doc.rust-lang.org/cargo/commands/cargo-metadata.html
    """

    def __init__(self, metadata):
        self.metadata = metadata
        self.pkgInfoById = {}
        self.pkgInfoByManifestPath = {}
        for info in metadata["packages"]:
            if info["name"] in EXCLUDED_PACKAGES:
                continue
            # Apply any hand-rolled fixups, carefully checking that they haven't been invalidated.
            if info["name"] in PACKAGE_METADATA_FIXUPS:
                fixups = PACKAGE_METADATA_FIXUPS[info["name"]]
                for key, change in fixups.items():
                    if info.get(key, None) != change["check"]:
                        assert False, "Fixup check failed for {}.{}: {} != {}".format(
                            info["name"], key,  info.get(key, None), change["check"])
                    if "fixup" in change:
                        info[key] = change["fixup"]
            # Index packages for fast lookup.
            assert info["id"] not in self.pkgInfoById
            self.pkgInfoById[info["id"]] = info
            assert info["manifest_path"] not in self.pkgInfoByManifestPath
            self.pkgInfoByManifestPath[info["manifest_path"]] = info
        # Add fake packages for things managed outside of cargo.
        for name, info in EXTRA_PACKAGE_METADATA.items():
            assert name not in self.pkgInfoById
            self.pkgInfoById[name] = info.copy()

    def has_package(self, id):
        return id in self.pkgInfoById

    def get_package_by_id(self, id):
        return self.pkgInfoById[id]

    def get_package_by_manifest_path(self, path):
        return self.pkgInfoByManifestPath[path]

    def get_package_dependencies(self, name, targets=None):
        """Get the set of dependencies for the named package, when compiling for the specified targets.
        
        This implementation uses `cargo build --build-plan` to list all inputs to the build process.
        It has the advantage of being guaranteed to correspond to what's included in the actual build,
        but requires using unstable cargo features.
        """
        cmd = (
            'cargo', '+nightly', '-Z', 'unstable-options', 'build',
            '--build-plan',
            '--quiet',
            '--locked',
            '--package', name
        )
        deps = set()
        for output in execute_for_each_target(cmd, targets):
            buildPlan = json.loads(output)
            for manifestPath in buildPlan["inputs"]:
                info = self.get_package_by_manifest_path(manifestPath)
                deps.add(info["id"])
        deps |= self.get_extra_dependencies_not_managed_by_cargo(name, targets, deps)
        return deps

    def get_extra_dependencies_not_managed_by_cargo(self, name, targets, deps):
        """Get additional dependencies for things managed outside of cargo.

        This includes optional C libraries like SQLCipher, as well as platform-specific
        dependencies for our various language bindings.
        """
        extras = set()
        if targets_include_android(targets):
            extras.add("ext-jna")
            extras.add("ext-protobuf")
        if targets_include_ios(targets):
            extras.add("ext-swift-protobuf")
        for dep in deps:
            name = self.pkgInfoById[dep]["name"]
            if name in PACKAGES_WITH_EXTRA_DEPENDENCIES:
                extras |= set(PACKAGES_WITH_EXTRA_DEPENDENCIES[name])
        return extras

    def get_dependency_summary(self, name=None, targets=None):
        """Print dependency and license summary infomation.

        Called with no arguments, this method will yield dependency summary information for the entire
        dependency tree.  When the `name` argument is specified it will yield information for just
        the dependencies of that package.  When the `targets` argument is specified it will yield
        information for the named package when compiled for just those targets.  Thus, each argument
        will produce a narrower list of dependencies.
        """
        if name is None:
            deps = set(id for id in self.pkgInfoById.keys())
        else:
            deps = self.get_package_dependencies(name, targets)
        for id in deps:
            if not self.is_external_dependency(id):
                continue
            yield self.get_license_info(id)

    def is_external_dependency(self, id):
        """Check whether the named package is an external dependency."""
        pkgInfo = self.pkgInfoById[id]
        try:
            if pkgInfo["source"] is not None:
                return True
        except KeyError:
            # There's no "source" key in info for externally-managed dependencies
            return True
        manifest = pkgInfo["manifest_path"]
        root = os.path.commonprefix([manifest, self.metadata["workspace_root"]])
        if root != self.metadata["workspace_root"]:
            return True
        return False

    def get_manifest_path(self, id):
        """Get the path to a package's Cargo manifest."""
        return self.pkgInfoById[id]["manifest_path"]

    def get_license_info(self, id):
        """Get the licensing info for the named dependency, or error if it can't be detemined."""
        pkgInfo = self.pkgInfoById[id]
        chosenLicense = self.pick_most_acceptable_license(id, pkgInfo["license"])
        return {
            "name": pkgInfo["name"],
            "repository": pkgInfo["repository"],
            "license": chosenLicense,
            "license_text": self._fetch_license_text(id, chosenLicense, pkgInfo),
        }

    def pick_most_acceptable_license(self, id, licenseId):
        """Select the best license under which to redistribute a dependency.

        This parses the SPDX-style license identifiers included in our dependencies
        and selects the best license for our needs, where "best" is a subjective judgement
        based on whether it's acceptable at all, and then how convenient it is to work with
        here in the license summary tool...
        """
        # Split "A/B" and "A OR B" into individual license names.
        licenses = set(l.strip() for l in re.split(r"\s*(?:/|\sOR\s)\s*", licenseId))
        # Try to pick the "best" compatible license available.
        for license in LICENES_IN_PREFERENCE_ORDER:
            if license in licenses:
                return license
        # OK now we're into the special snowflakes, and we want to be careful
        # not to unexpectedly accept new dependencies under these licenes.
        if "OpenSSL" in licenses:
            if id == "ext-openssl":
                return "OpenSSL"
        raise RuntimeError("Could not determine acceptable license for {}; license is '{}'".format(id, licenseId))

    def _fetch_license_text(self, id, license, pkgInfo):
        if "license_text" in pkgInfo:
            return pkgInfo["license_text"]
        licenseFile = pkgInfo.get("license_file", None)
        if licenseFile is not None:
            if licenseFile.startswith("https://"):
                r = requests.get(licenseFile)
                r.raise_for_status()
                return r.content.decode("utf8")
            else:
                pkgRoot = os.path.dirname(pkgInfo["manifest_path"])
                with open(os.path.join(pkgRoot, licenseFile)) as f:
                    return f.read()
        # No explicit license file was declared, let's see if we can unambiguously identify one
        # using common naming conventions.
        pkgRoot = os.path.dirname(pkgInfo["manifest_path"])
        try:
            licenseFileNames = COMMON_LICENSE_FILE_NAMES[license]
        except KeyError:
            licenseFileNames = COMMON_LICENSE_FILE_NAMES[""]
        foundLicenseFiles = [nm for nm in os.listdir(pkgRoot) if nm.lower() in licenseFileNames]
        if len(foundLicenseFiles) == 1:
            with open(os.path.join(pkgRoot, foundLicenseFiles[0])) as f:
                # Try to guess at where to download the license file.
                return f.read()
        # Not unambiguous, but a human can probably pick the right one.
        if len(foundLicenseFiles) > 1:
            raise RuntimeError("Multiple ambiguous license files found for {}: {}".format(
                pkgInfo["name"], foundLicenseFiles))
        # No license file at all? You'll have to look it up from the internet.
        raise RuntimeError("Could not find license file for '{}'; try checking {}".format(
            pkgInfo["name"], pkgInfo["repository"]))
    

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="summarize dependencies and license information")
    parser.add_argument('-p', '--package', action="store")
    parser.add_argument('--target', action="append", dest="targets")
    parser.add_argument('--all-android-targets', action="append_const", dest="targets", const=ALL_ANDROID_TARGETS)
    parser.add_argument('--all-ios-targets', action="append_const", dest="targets", const=ALL_IOS_TARGETS)
    parser.add_argument('--json', action="store_true", help="output JSON rather than human-readable text")
    parser.add_argument('--check', action="store", help="suppress output, instead checking that it matches the given file")
    args = parser.parse_args()
    if args.targets:
        if args.package is None:
            raise RuntimeError("You must specify a package name when specifying targets")
        # Flatten the lists introduced by --all-XXX-targets options.
        args.targets = list(itertools.chain(*([t] if isinstance(t, str) else t for t in args.targets)))

    metadata = get_workspace_metadata()
    deps = metadata.get_dependency_summary(args.package, args.targets)

    if args.check:
        output = io.StringIO()
    else:
        output = sys.stdout

    if args.json:
        json.dump([info for info in deps], output)
    else:
        print_dependency_summary(deps, file=output)

    if args.check:
        with open(args.check) as f:
            if f.read() != output.getvalue():
                raise RuntimeError("Dependency details have changed from those in {}".format(args.check))

