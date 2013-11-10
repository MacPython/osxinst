#!/usr/bin/env python

from distutils.core import setup
import bdist_osxinst.bdist_osxinst

config = """
[:globals:]
title = Distutils extension: bdist_osxinst

[bdist_osxinst]
description = This package contains the distutils 'bdist_osxinst' command
              which can be used to create OSX installer packages.
"""

setup(name = "bdist_osxinst",
      version = "0.9.0",
      description = "Distutils extension to create OSX installer packages",
      author = "Matthias Baas",
      author_email = "mbaas@users.sourceforge.net",
      license = "Revised BSD License",
      url = "https://github.com/MacPython/osxinst",
      packages = ["bdist_osxinst"],
      cmdclass = {"bdist_osxinst":bdist_osxinst.bdist_osxinst.bdist_osxinst},
      command_options = {"bdist_osxinst" : {"license":("setup.py","license.rtf"),
                                            "config_str":("setup.py",config)}}
)
