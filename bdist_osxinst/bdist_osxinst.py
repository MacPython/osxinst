# Distutils command to create OSX installer packages
#
# Notes: Receipts in /var/db/receipts

import sys, os, os.path, subprocess, shutil, base64
from distutils.core import Command
from distutils.util import get_platform
from distutils.dir_util import remove_tree
from distutils.errors import *
from distutils.sysconfig import get_config_var
from distutils import log
# Python3 modules:
if sys.version_info[0]>=3:
    from urllib.parse import urlparse
    from configparser import ConfigParser
    from io import StringIO
# Python2 modules:
else:
    from urlparse import urlparse
    from ConfigParser import ConfigParser
    from StringIO import StringIO


class Package:
    """Contains all data to produce an individual component package.
    """
    def __init__(self, name, identifier, version, title, description, stage_root, install_location):
        # The file name of the *.pkg file (without path)
        self.name = name
        # A package identifier string.
        self.identifier = identifier
        # A package version string.
        self.version = version
        # The title for the package. This is displayed in the installer GUI as the name of the package.
        self.title = title
        # A description of the package. This is displayed in the installer GUI.
        self.description = description
        # The root directory within the stage area containing the source files that should be packaged up
        self.stage_root = stage_root
        # The absolute install location (such as "/Library/Frameworks/Python.framework/Versions/3.3/lib/python3.3/site-packages")
        self.install_location = install_location


def get_python_arch():
    """Returns the default value for the hostArchitectures xml attribute.
    
    Uses the Python CFLAGS config var to find the -arch options and
    returns a corresponding value for the hostArchitectures attribute.
    The return value should not be used if the installer package
    only contains pure Python modules.
    """
    flags = get_config_var("CFLAGS")
    i386 = "-arch i386" in flags
    x86_64 = "-arch i86_64" in flags
    ppc = "-arch ppc" in flags
    
    archs = []
    if i386:
        # This matches 32bit and 64bit
        archs.append("i386")
    elif x86_64:
        # This matches only 64bit
        archs.append("x86_64")
    if ppc:
        archs.append("ppc")
    
    return ",".join(archs)


class bdist_osxinst(Command):
    """Create an installer package for OSX.
    
    This command builds a flat OSX installer package using the pkgbuild
    and productbuild command line utilities. By default, it puts every
    top-level Python package into a separate component package so that
    at installation time the user can see what packages get installed
    (this behaviour can be overridden using the --single-lib-pk option).
    
    Dev notes:
    
    See Apple's "Distribution Definition XML Schema Reference" for information
    about the tags that can appear in the distribution xml file.
    https://developer.apple.com/library/mac/#documentation/DeveloperTools/Reference/DistributionDefinitionRef/ 
    """

    description = "create an installer package for OSX"

    # List of option tuples: long name, short name (None if no short
    # name), and help string.
    user_options = [("bdist-dir=", None,
                     "temporary directory for creating the distribution"),
                    ('title=', 't',
                     "title to display inside the installer instead of default"),
                    ('welcome=', 'w',
                     "welcome file that should be displayed during installation"),
                    ("readme=", 'r',
                     "readme file that should be displayed during installation"),
                    ("license=", 'l',
                     "license file that should be displayed during installation"),
                    ('dist-dir=', 'd',
                     "directory to put final built distributions in"),
                    ('skip-build', None,
                     "skip rebuilding everything (for testing/debugging)"),
                    ('keep-temp', 'k',
                     "keep the pseudo-installation tree around after " +
                     "creating the distribution archive"),
                    ('config-file=', 'c',
                     "config file describing packages"),
                    ('config-str=', None,
                     "config file given as a string"),
                    ('arch=', None,
                     "required host architecture (default: %s). This is "%(get_python_arch())+
                     "only used when the distribution contains extension modules."),
                    ('single-lib-pkg', None,
                     "only create one single package for all Python packages and modules")
                   ]

    boolean_options = ['keep-temp', 'skip-build', 'single-lib-pkg']

    def initialize_options(self):
        self.bdist_dir = None
        self.dist_dir = None
        self.title = None
        self.welcome = None
        self.readme = None
        self.license = None
        self.skip_build = None
        self.keep_temp = None
        self.config_file = None
        self.config_str = None
        self.arch = None
        self.single_lib_pkg = None
        
        self.id_prefix = None
        self.config = ConfigParser()

    def finalize_options(self):

        if self.bdist_dir is None:
            bdist_base = self.get_finalized_command('bdist').bdist_base
            self.bdist_dir = os.path.join(bdist_base, 'osxinst')
        
        self.bdist_dir = os.path.normpath(self.bdist_dir)

        if self.config_str is not None:
            self.config.readfp(StringIO(self.config_str))
        
        if self.config_file is not None:
            f = open(self.config_file, "rt")
            self.config.readfp(f)
            f.close()

        if self.title is None:
            self.title = self.get_config_value("title", default=self.distribution.get_name())
        
        if self.welcome is None:
            self.welcome = self.get_config_value("welcome", default=None)

        if self.readme is None:
            self.readme = self.get_config_value("readme", default=None)

        if self.license is None:
            self.license = self.get_config_value("license", default=None)
        
            
        if self.arch is None:
            self.arch = get_python_arch()

        self.set_undefined_options('bdist', ('dist_dir', 'dist_dir'))
        
        # Determine the prefix for package ids
        # (this is the reverse of the network location (without port) of the url)
        url = self.distribution.get_url()
        netloc = urlparse(url)[1].split(":")[0]
        self.id_prefix = ".".join(reversed(netloc.split(".")))

    def run(self):
        """Create the OSX installer package.
        """
        if sys.platform!="darwin":
            raise DistutilsPlatformError("OSX installer package must be created on an OSX platform")

        # Make sure everything is built
        if not self.skip_build:
            self.run_command('build')

        # The path to the "stage" dir where the temp installation will be done
        stage_dir = os.path.join(self.bdist_dir, "stage")
        # The path to the "stage_mod" dir where top-level modules or data files will be copied
        stage_mod_dir = os.path.join(self.bdist_dir, "stage_mod")
        # The path to the "pkgs" dir where the individual component packages will be put
        pkgs_dir = os.path.join(self.bdist_dir, "pkgs")
        # The path to the "resources" dir where the resources for the final product package will be put
        resources_dir = os.path.join(self.bdist_dir, "resources")
        # The output path for the distribution xml file for the product package
        dist_xml_file = os.path.join(self.bdist_dir, "Distribution")
        # The name of the final product package
        if self.distribution.has_ext_modules():
            pkg_base_name = "%s.%s-py%d.%d.pkg"%(self.distribution.get_fullname(), get_platform(), sys.version_info[0], sys.version_info[1])
        else:
            pkg_base_name = "%s.macosx-py%d.%d.pkg"%(self.distribution.get_fullname(), sys.version_info[0], sys.version_info[1])
        product_pkg_name = os.path.join(self.dist_dir, pkg_base_name)

        # Install everything into the temp area...
        log.info("installing to %s", stage_dir)
        stage_lib_dir, stage_scripts_dir = self.do_install(install_root=stage_dir)

        # Get the absolute target path where the installer will put the files.
        # target_lib_dir typically is:     /Library/Frameworks/Python.framework/Versions/<ver>/lib/python<ver>/site-packages
        # target_scripts_dir typically is: /Library/Frameworks/Python.framework/Versions/<ver>/bin
        target_lib_dir = self.stage_dir_to_install_dir(stage_lib_dir, stage_dir)
        target_scripts_dir = self.stage_dir_to_install_dir(stage_scripts_dir, stage_dir)
        
        log.info("Target lib dir: %s"%target_lib_dir)
        log.info("Target scripts dir: %s"%target_scripts_dir)
        
        # Create the Package objects...
        pkgs = self.create_package_objs(stage_lib_dir, stage_mod_dir, stage_scripts_dir, target_lib_dir, target_scripts_dir)

        # Open the shell script file which will contain the commands to generate
        # the packages. The script may be used by the user to regenerate the package.
        sh_file = open(os.path.join(self.bdist_dir, "mkpkg.sh"), "wt")
        sh_file.write("# Create '%s' installer package.\n"%self.distribution.get_name())
        sh_file.write("# (if you want to run this script you need to be in the root directory of the package)\n\n")

        # Build the individual OSX installer packages and put them into the pkgs dir..
        sh_file.write('# Build component packages\n')
        sh_file.write('mkdir -p "%s"\n'%pkgs_dir)
        if not os.path.exists(pkgs_dir):
            os.mkdir(pkgs_dir)

        for pkg in pkgs:
            log.info("Create component package '%s'"%pkg.name)
            pkg_name = os.path.join(pkgs_dir, pkg.name)
            cmd = self.pkgbuild(pkg_name, root=pkg.stage_root, identifier=pkg.identifier, version=pkg.version, install_location=pkg.install_location)
            sh_file.write("%s\n"%cmd)

        # Initialize the resources dir...
        self.init_resources(resources_dir)

        # Create the final product package...
        sh_file.write('\n# Build product package\n')
        sh_file.write('mkdir -p "%s"\n'%self.dist_dir)
        if not os.path.exists(self.dist_dir):
            os.makedirs(self.dist_dir)

        self.create_distribution_xml(dist_xml_file, target_lib_dir = target_lib_dir, pkgs=pkgs)
        cmd = self.productbuild(product_pkg_name, distribution=dist_xml_file, package_path=pkgs_dir, resources=resources_dir)
        sh_file.write("%s\n"%cmd)

        # Remove temp directory...
        if not self.keep_temp:
            remove_tree(self.bdist_dir, dry_run=self.dry_run)
        
        sh_file.close()

    def create_package_objs(self, stage_lib_dir, stage_mod_dir, stage_scripts_dir, target_lib_dir, target_scripts_dir):
        """Create the Package objects that represent the component packages.
        """
        pkgs = []
        
        if not self.single_lib_pkg:
            # Check what we actually have to install...
            pkgNames,files,dirNames = self.get_installed_contents(stage_lib_dir)
            log.info("%s packages, %s files, %s directories"%(len(pkgNames), len(files), len(dirNames)))
            
            # Create Package objects for all top-level Python packages...        
            if len(pkgNames)!=0:
                libPkgs = self.create_lib_packages(pkgNames, stage_lib_dir, target_lib_dir)
                pkgs.extend(libPkgs)
            
            # Create packages for top-level modules or data files/directories...
            if len(files)!=0 or len(dirNames)!=0:
                self.copy_mods_and_data(files, dirNames, stage_lib_dir, stage_mod_dir)
                pkg = self.create_mods_package(stage_mod_dir, target_lib_dir)
                pkgs.append(pkg)
        else:
            # Create a single Package object for the libs...
            if self.distribution.has_modules():
                name = self.distribution.get_name()
                pkg = self.create_single_lib_package(name, stage_lib_dir, target_lib_dir)
                pkgs.append(pkg)

        # Create a Package object for the scripts...
        if self.distribution.has_scripts():
            pkg = self.create_script_package(stage_scripts_dir, target_scripts_dir)
            pkgs.append(pkg)
        
        return pkgs

    def do_install(self, install_root):
        """Install the package into a temporary install location.
        
        install_root is the path where the package should be installed.
        Returns the lib dir and the script dir within the stage area
        where things got installed.
        """
        install = self.reinitialize_command('install', reinit_subcommands=1)
        install.root = install_root
        install.skip_build = self.skip_build
        install.warn_dir = 0

        install_scripts = self.reinitialize_command('install_scripts')
        install_lib = self.reinitialize_command('install_lib')

        install_scripts.ensure_finalized()
        install_lib.ensure_finalized()
        install.ensure_finalized()

        stage_scripts_dir = install_scripts.install_dir
        stage_lib_dir = install_lib.install_dir

        # Make sure the lib dir points to the root of the site-packages directory...
        while os.path.basename(stage_lib_dir)!="site-packages":
            if "site-packages" not in stage_lib_dir:
                raise DistutilsInternalError("unexpected lib install directory path")
            stage_lib_dir = os.path.dirname(stage_lib_dir)
                    
        install.run()
        
        return stage_lib_dir, stage_scripts_dir

    def stage_dir_to_install_dir(self, stage_dir, stage_root):
        """Convert a local stage dir into an absolute target install path.
        
        stage_dir is the directory path that should be converted. It has to
        be a location within the stage area. stage_root is the root of the
        stage area. Basically, all this method does, is remove the stage_root
        prefix from stage_dir. 
        """
        # Check that stage_dir is really inside stage_root
        if not stage_dir.startswith(stage_root):
            raise DistutilsInternalError("unexpected temp install directory path")

        install_dir = os.path.normpath(stage_dir[len(stage_root):])

        # Make sure we have an absolute path now...
        if not os.path.isabs(install_dir):
            raise DistutilsInternalError("target install path not absolute: %s"%install_dir)
        
        return install_dir

    def copy_mods_and_data(self, files, dirNames, stage_lib_dir, stage_mod_dir):
        """Copy top-level modules and non-package directories into a separate stage area.
        
        files is a list of file names (without path). dirNames is a list of
        directories (without path). The files and directories must be located
        in stage_lib_dir. stage_mod_dir is the destination path where the
        files and directories will be copied. If the path doesn't exist,
        it is created.
        
        The contents of stage_mod_dir can be used as root for a installer
        package that only installs those files and directories without
        installing other Python packages.
        """
        if not os.path.exists(stage_mod_dir):
            os.mkdir(stage_mod_dir)
            
        # Copy files...
        for fileName in files:
            src = os.path.join(stage_lib_dir, fileName)
            dst = os.path.join(stage_mod_dir, fileName)
            shutil.copy2(src, dst)
        
        # Copy directories...
        for dirName in dirNames:
            src = os.path.join(stage_lib_dir, dirName)
            dst = os.path.join(stage_mod_dir, dirName)
            if os.path.exists(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        
    def create_script_package(self, stage_scripts_dir, target_scripts_dir):
        """Create a Package object for the scripts.
        
        stage_scripts_dir is the temp install folder where the scripts
        are located (the bin directory in the stage area).
        target_scripts_dir is the absolute path to the directory where the
        scripts should be installed when the generated package is installed
        by the user.
        """
        title = self.get_config_value("title", section=":scripts:", default="Scripts")
        description = self.get_config_value("description", section=":scripts:", default="This package contains command line scripts.")
        scriptsPkg = Package(name = "scripts.pkg",
                             identifier = self.get_identifier("%s-scripts"%self.distribution.get_name()),
                             version = self.distribution.get_version(),
                             title = title,
                             description = description,
                             stage_root = stage_scripts_dir,
                             install_location = target_scripts_dir)
        return scriptsPkg

    def create_mods_package(self, stage_mods_dir, target_lib_dir):
        """Create a Package object for the top-level modules and data files/dirs.
        
        stage_mods_dir is the temp install folder where the modules and data files/dirs
        are located.
        target_lib_dir is the absolute path to the directory where the
        modules should be installed when the generated package is installed
        by the user.
        """
        title = self.get_config_value("title", section=":mods:", default="Modules")
        description = self.get_config_value("description", section=":mods:", default="This package contains top-level modules and data files.")
        pkg = Package(name = "modules.pkg",
                      identifier = self.get_identifier("%s-mods"%self.distribution.get_name()),
                      version = self.distribution.get_version(),
                      title = title,
                      description = description,
                      stage_root = stage_mods_dir,
                      install_location = target_lib_dir)
        return pkg

    def create_lib_packages(self, pkgNames, stage_lib_dir, target_lib_dir):
        """Create Package objects for all top-level Python packages.
        
        pkgNames is a list of top-level Python package names. Every name
        will be turned into a Package object.
        stage_lib_dir is the temp install folder where the Python packages
        are located (the site-packages directory in the stage area).
        target_lib_dir is the absolute path to the directory where the
        packages should be installed when the generated package is installed
        by the user.
        """
        pkgs = []
        version = self.distribution.get_version()

        # Create a Package object for every toplevel Python package...        
        for name in pkgNames:
            file_name = "pkg.%s.pkg"%(name)
            title = self.get_config_value("title", section=name, default="%s package"%name)
            description = self.get_config_value("description", section=name, default='Python package "%s".'%name)
            pkg = Package(name = file_name,
                          identifier = self.get_identifier(os.path.splitext(file_name)[0]),
                          version = version,
                          title = title,
                          description = description,
                          stage_root = os.path.join(stage_lib_dir, name),
                          install_location = os.path.join(target_lib_dir, name))
            pkgs.append(pkg)
    
        return pkgs
    
    def create_single_lib_package(self, name, stage_lib_dir, target_lib_dir):
        """Return a Package object for the entire lib directory.
        
        name is the name of the package. The name does not have to refer to
        any directory name inside the lib directory, it is only used for
        the installer package name and for the title and description
        (and for looking up values from the config file).
        stage_lib_dir is the temp install folder where the Python packages
        are located (the site-packages directory in the stage area).
        target_lib_dir is the absolute path to the directory where the
        packages should be installed when the generated package is installed
        by the user.
        """
        version = self.distribution.get_version()
        file_name = "pkg.%s.pkg"%(name)
        title = self.get_config_value("title", section=name, default="%s package"%name)
        description = self.get_config_value("description", section=name, default='Python packages and modules.')
        pkg = Package(name = file_name,
                      identifier = self.get_identifier(os.path.splitext(file_name)[0]),
                      version = version,
                      title = title,
                      description = description,
                      stage_root = stage_lib_dir,
                      install_location = target_lib_dir)
        return pkg
        
    
    def get_installed_contents(self, stage_lib_dir):
        """Return the contents of a Python module directory.
        
        Returns a tuple (pkgNames, fileNames, dirNames) where pkgNames
        is a list of top-level Python package names, fileNames is a list
        containing the files directly located in the input directory and
        dirNames is a list of non-package directories.
        """
        pkgs = []
        files = []
        dirs = []
        for name in os.listdir(stage_lib_dir):
            fullName = os.path.join(stage_lib_dir, name)
            if os.path.isdir(fullName):
                initFile = os.path.join(fullName, "__init__.py")
                if os.path.isfile(initFile):
                    pkgs.append(name)
                else:
                    dirs.append(name)
            else:
                if os.path.splitext(name)[1] not in [".egg-info"]:
                    files.append(name)
                    
        return pkgs,files,dirs

    def init_resources(self, resources_dir):
        """Initialize and populate the resource directory required for calling productbuild.
        """
        if not os.path.exists(resources_dir):
            os.mkdir(resources_dir)

        # Copy welcome, readme and license files...
        for res_file in [self.welcome, self.readme, self.license]:
            if res_file is not None:
                shutil.copy(res_file, resources_dir)

        if self.welcome is None:
            file_name = os.path.join(resources_dir, "welcome.html")
            self.create_welcome_file(os.path.join(resources_dir, "welcome.html"))
            self.welcome = file_name

        # Write the background image
        f = open(os.path.join(resources_dir, "background-dimmed.png"), "wb")
        f.write(base64.b64decode(_background_image))
        f.close()
    
    def create_welcome_file(self, file_name):
        """Create the default welcome html file.
        
        file_name is the output file name.
        """
        dist = self.distribution
        name = dist.get_name()
        version = dist.get_version()
        url = dist.get_url()
        license = dist.get_license()
        
        info = [("Package:", name), ("Version:", version)]
        if license!="UNKNOWN":
            info.append(("License:", license))
        if url!="UNKNOWN":
            info.append(("Homepage:", '<a href="%s">%s</a>'%(url, url)))
        
        w = open(file_name, "wt")
        w.write('<html>\n')
        w.write('<head>\n')
        w.write('<meta http-equiv="Content-type" content="text/html;charset=UTF-8">\n')
        w.write('<style type="text/css">\n')
        w.write('body { font-family: sans-serif; }\n')
        w.write('</style>\n')
        w.write('</head>\n')
        w.write('<body>\n')
        w.write('<h1>%s</h1>\n'%self.title)
        w.write('<table border=0 cellspacing=0 cellpadding=1>\n')
        for label,value in info:
            w.write('  <tr><td><em>%s</em>&nbsp;</td><td><b>%s</b></td></tr>\n'%(label,value))
        w.write('</table>\n')
        w.write('<hr>\n')
        w.write('<p>This will install %s v%s for Python %d.%d.</p>\n'%(name, version, sys.version_info[0], sys.version_info[1]))
        w.write('<p>Note that you can only install this package if you are using\n')
        w.write('the Python version from <a href="http://www.python.org/">www.python.org</a>.</p>\n')
        w.write('</body>\n')
        w.write('</html>\n')
        w.close()

    def productbuild(self, pkg_name, distribution, package_path, resources):
        """Wrapper for calling the productbuild command line tool.
        """
        cmd = 'productbuild --distribution "%s" --package-path "%s" --resources "%s" "%s"'%(distribution, package_path, resources, pkg_name)
        self.call(cmd)
        return cmd
        
    def pkgbuild(self, pkg_name, root, identifier, version, install_location):
        """Wrapper for calling the pkgbuild command line tool.
        """
        cmd = 'pkgbuild --root "%s" --identifier "%s" --version %s --install-location "%s" "%s"'%(root, identifier, version, install_location, pkg_name)
        self.call(cmd)
        return cmd

    def get_identifier(self, name):
        """Build a package identifier string for a package with the given name.
        
        The method prefixes the name with the d_prefix (which is based on the
        package url) and adds the Python version as suffix.
        """
        identifier = "%s_%s_py%d.%d"%(self.id_prefix, name, sys.version_info[0], sys.version_info[1])
        return identifier

    def get_config_value(self, key, section=":globals:", default=None):
        """Return a value from the config file.

        Return the value with the given key in the given section. If the
        value doesn't exist, the default value is returned.
        """
        if self.config.has_option(section, key):
            return self.config.get(section, key)
        else:
            return default

    def create_distribution_xml(self, filename, pkgs, target_lib_dir):
        """Create the distribution xml file.
        
        filename is the output file name of the xml file.
        
        pkgs is a list of Package objects describing the individual component
        packages that will be contained in the generated package. 
        
        target_lib_dir is the directory that is used to check if the required
        Python version is installed. This path is checked at installation
        time and if it does not exist, the package can not be installed.
        """
        xml = ['<?xml version="1.0" ?>']
        xml += ['<installer-gui-script minSpecVersion="1">']
        xml += ['<title>%s</title>'%self.title]
        xml += ['<domain enable_anywhere="true" enable_currentUserHome="false" enable_localSystem="true"/>']
        if self.distribution.has_ext_modules():
            xml += ['<options hostArchitectures="%s"/>'%self.arch]
        xml += ['<background file="background-dimmed.png" uti="public.png" alignment="left" scaling="proportional"/>']
        xml += ['<volume-check script="checkForPythonInstall()"/>']
        # The welcome/readme/license files options refer to the original files.
        # The xml file will only contain the base name though because it's assumed
        # the files will be copied directly into the resources folder. 
        if self.welcome is not None:
            uti = self.get_file_uti(self.welcome)
            xml += ['<welcome file="%s" uti="%s"/>'%(os.path.basename(self.welcome), uti)]
        if self.readme is not None:
            uti = self.get_file_uti(self.readme)
            xml += ['<readme file="%s" uti="%s"/>'%(os.path.basename(self.readme), uti)]
        if self.license is not None:
            uti = self.get_file_uti(self.license)
            xml += ['<license file="%s" uti="%s"/>'%(os.path.basename(self.license), uti)]

        xml += ['<choices-outline>']
        for i in range(len(pkgs)):
            xml += ['  <line choice="choice%d"/>'%(i+1)]
        xml += ['</choices-outline>']

        for i,pkg in enumerate(pkgs):
            desc = pkg.description.replace("\n", " ")
            xml += ['<choice id="choice%d" title=%s description=%s>'%(i+1, repr(pkg.title), repr(desc))]
            xml += ['  <pkg-ref id="%s"/>'%pkg.identifier]
            xml += ['</choice>']

        for i,pkg in enumerate(pkgs):
            xml += ['<pkg-ref id="%s" version="%s">%s</pkg-ref>'%(pkg.identifier, pkg.version, pkg.name)]

        xml += ["""
<script>
<![CDATA[
function checkForPythonInstall()
{
    if (system.files.fileExistsAtPath(my.target.mountpoint+"%(site_packages_dir)s"))
    {
        return true;
    }
    else
    {
        my.result.type = "Fatal";
        my.result.message = "Python %(python_ver)s (from www.python.org) is not installed on this volume.";
        return false;
    }
}
]]>
</script>"""%{"site_packages_dir":target_lib_dir, "python_ver":"%d.%d"%(sys.version_info[:2])}]

        xml += ["</installer-gui-script>"]
        
        f = open(filename, "wt")
        f.write("\n".join(xml))
        f.close()

    def get_file_uti(self, file_name):
        """Determine the Uniform Type Identifier of a file using the mdls command line tool.
        
        file_name is the name of the file whose uti should be returned.
        The uti is required for some tags in the distribution xml file.
        """
        # Suffix html? Then don't bother calling mdls.
        # (this is also to "fix" a problem when calling mdls on the newly
        # generated welcome.html. In this case, mdls returns "(null)" for
        # some reason...!?)
        if os.path.splitext(file_name)[1].lower()==".html":
            return "public.html"
        
        cmd = 'mdls -name kMDItemContentType -raw "%s"'%(file_name)
        uti = self.call(cmd)
        uti = uti.decode("ascii")
        if "." not in uti:
            raise DistutilsExecError("Invalid uti for file '%s': '%s'"%(file_name, uti))
        return uti

    def call(self, cmd):
        """Run a command line and return its result.
        """
        log.info(cmd)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
        out,err = proc.communicate()
        if proc.returncode!=0:
            log.error("ERROR running command:")
            if hasattr(out, "decode"):
                out = out.decode("utf-8")
            if hasattr(err, "decode"):
                err = err.decode("utf-8")
            log.error(out)
            log.error(err)
            raise DistutilsExecError("Running a system command failed")
        return out


# base64 encoded background image (png format)
_background_image = \
  b'iVBORw0KGgoAAAANSUhEUgAAARwAAAEKCAYAAADAe+pmAAABhGlDQ1BJQ0MgUHJvZmlsZQAAKM/Vkr9Lw0AUx79JlS5FOxRBpywWh7bUVgUXoWYQQbCWDlVc'\
  b'0iRNKzUNyVkVF0cXQVAnHfwB4j8gOIngPyAUOqmze8FFS3zJFdulg+DiF473uce7d8e7LxDIK5ZVFcPApsns3MK8VFhdk4KvEBGEL0V1rEw2u4S++mhC8GIj'\
  b'7vXC7xTSdEcFhAgxUy2bER8QP28zi1gsEEdsehSx1zticN73uMj5xK/J52TiW+Khump4Z++Jk6ZWMYnfiefUsqIBgQniWLGn3uhh/h5fY7JSrRRthema5I1G'\
  b'rlVrtmMpqo6/FdN3mBflmrVrV4wykzI0ST0mLZpqIialkpNp/Cd5HuLUWvG9IYw0urnzUWD5Bhj46uamDoGLODB82s1Fk0A4ATycqVt2vdNaEGkU/H/5nnvP'\
  b'J/Rj7gFfM8B1E8jvAdNPwB2taInuXQeyIcrPQkyLP6sjp5RO8V4h8sngm+u2xoHgMdA+ct3PS9dtX5GvXoDHjW9BRWmt8CW7OwAAAAZiS0dEAP8A/wD/oL2n'\
  b'kwAAAAlwSFlzAAAK8AAACvABQqw0mAAAIABJREFUeNrsvWmwZdlVHvittc+9b8p8OVdmZc2lCU1IQkIMbWwsMchoAAS0AZupCbcDt4MwyEK0o+1wdEdHtCdw'\
  b'04jCjsCN7ZaxEQIzSkIyGIwZxFASaK5JVZWlrMzKfDm8+d6z9+ofe6291z7vZaoQGqqyzo6oyvfenc699+zvrPWtb30LGNe4xjWucY1rXOMa17jGNa5xjWtc'\
  b'4xrXuMY1rnGNa1zjGte4xjWucY1rXOMa17jGNa5xjWtc4xrXuMY1rnGNa1zjGte4xjWucY1rXOMa17jGNa5xjWtc4xrXuMY1rnGNa1zjGte4xjWucY1rXOMa'\
  b'17jGNa5xjWtc4xrXuMY1rnGNa1zjGte4xjWucY1rXOMa17jGNa5xjWtc4xrXuMY1rnGN6xm9aPwIbvz19W/+idMRstgnOjiLtGJ/34x0oI8URE8EgeDKjCbz'\
  b'FAAIAED0FgjADCQBCAKIQEBYnSRZ7KTXOwMQHF6QKxBASDAlbE5Y1gFgGuTsO/7p92+P38gIOON6Gq9vfMtPHJ0nOjwXPj4TLG32YbmP4CvzLsSUcOzgEk4c'\
  b'WsLBpSluPrKMhUmHWR9x6vAKjq0uIjBBBIhJcPORFRxaWQATQRRo9pwtAhABKQnOX9nC2sYOmAAmwubuHA988hKmXcAsRpy9uIGt3Tm2d3ucuXAVfYyYBuDQ'\
  b'JM0DIa5M0iZD5hOWC1NOZ9/xz/7eCEgj4IzrqbJe9+Z77t6NdGwr0ur6jBcvzUIABEyMwytTnDq6ghOryzh2cBGHlhdweGUBXcdYWZhgOmEcWJwiMCEmwfLC'\
  b'BMsLE1ABEgER1d/zn8piyvhD7m8JGXjstlkfcXljB11gxJSwsT1DHxNmfcLmzgwxCS5e3cKljR1cWt/CpY1dXLy6hbWr24iSAAAnFuP8wBRbSwGXlzt59Bf+'\
  b'xd9bG7/5EXDG9TkCmK2eT12e86HzW2FCTFjoGCcPr+DUkfzfTYeXsLwwwcGlKY4cWMTBpfyzB4ycNhGYgKR/ZP320wBU/O/XWna/J3t/l5yBAMSUcHljF7vz'\
  b'Oa5uzXDx6hZmfcTZtQ188sJVnL1wBY+vbSAi4dgixcOLsnFkKvf/4o/8wGPjWTECzrg+Q+ubfvgnljbn/Nz1OR9/fCes7EaAiHHi0DJuP34Qtx0/iFuOH8Sx'\
  b'g0s4trqEwysLIKo8S+VjctojAFi/5sy65FTJgIgHZ0C6xt/ttv3+bmlXAsAu8kHzewYc2ue17IBZ07S19S1cuLqFT164igfPXsQj567gwbMXAQBHFhFPr8iZ'\
  b'A1N8/B3//AfG9GsEnHF9Ouvrf+ie0xd2w/Mf3w4ru5HQMeOmI8u4/fgq7r75CG49sYrbjh/EgcWpi1gaeNkHBWjAvewfihARRORTnigDfIAol7Pffez3JACR'\
  b'7A9Swwe0v0AApJTwyOOX8eFHzuFDD53Dx8+cR98nLHTAHQdx4eiC3PuOH/nBEXhGwBnXk02Zzm53zzu3HSYA0AXG8249jufecgRfcOtxnDp6AMsLk/2B5Drp'\
  b'i92npjztDieLekiG+/wzuK7x3Pugl0/1FAUrqun9Lq9v4977zuAD938SH3v0PObziOlE8NzD8si7f+xN7x/PphFwxnWN9cYfvufowxvdlxjQEBHuPHkYL77r'\
  b'Jrzi2Tfj+OGVAgp+/10LHIgEIrRvoCPuQUT5WaWwvwRIJn2j7H9CGGgR7Q2ShhyOpW0F2OiagVWDjWxpWEnppMEbuNe/tLGJ933kYfzO+x/AhSsbEAhOLmF+'\
  b'2yr+9Bd/9E0jxzMCzrj8evUP/usvv299ehyaMqwuL+Dlz70Ff+lFd+Cmwyt7sg26fhBTv0wfUOyT31hmk6MahQWS654Y5VjcQeXH1Z8NaJr0TgaAQ/sct8/6'\
  b'3LELScHCGiRJjcr0L396/xn85h99FPefeULvSHj+cTz+3h9/0/vGs2wEnGf8+uYfvmfpE5vTrzy/Y1ENcOroQbz6Zc/CS599GiL5Si8GEPsyGwOOBJWYtZVc'\
  b'lJCFer7ULQ35az+7zKVhhey1h69T7r+HdJaGWN7v+IZvKrnX9Y8XXPu5k4Ldpaub+LX//gH8yUcfgShVfctB2rntAP7bO37kTSO38xRbYfwIPkdg87/es/TA'\
  b'5sKrLuyEzv52dHUZr/uyF+AFd55CTEqSIqc2InlTRf03Df5mmy4mvb/+3mt5KAqQdBsnACKCXl8jSn2MPc7+LgB696+/X4S7Hw3vI+gF+lr17/64+9S+dj98'\
  b'f/rcsPsLkJLdLuU9J3dM0+kUd996EpCEx564hJQE67vSbfVy51e86jVnPvJ7v96PZ98IOM88sFlfeNWleResIjSdBHzdl74Az739JERBISUV0l3nPw8SUerf'\
  b'/GOjA5aUMlAkB0rJPUeS+liRel8PFPv9V8FE9HfKv+vrSXL3HR6ff373vvqUy/VRqIKuf//695T0WPV5Qgh41u03o08Jj569ABHBdg/e6tMIOiPgPPPW4Ve8'\
  b'8VVP7E6m9jsz44ueexte/gV3AMR5w6W6MePgim8bF7Z5sXfzeqBITfSTaVyLLsRtdvu999FTqhGUAVeJTPxriOT7ApBESKrtiahAOIzUYMeWcoTUu/cbNQ+M'\
  b'yQEd6vEkyUBtx23RmkVCvQC3njqOs+fXcPHyOgAFnbnc+RV/9WvPfOT3R9B5Kqxu/Ag+u+tr//6//uKPrU8WPXexsjTFK15wF8AB81T/TkO2VvkW6GZOkjLP'\
  b'QwRiyv8qiVK0fwNtjDS1rgwInh+ha/BCUQYEiz6ocjvUVI484UP+yVz6w3CVtqT9WPocpCmWP9qYJF8RC7lM5TMh2ofkoYBXf9kX4Ym1K7h0ZR0AcHFHwkNX'\
  b'0lcCeOd4Nn7+10gaf5ZTqT+9vPzVs0RNReauW47jdX/pZei6sO8XEFPCfN5jc3sXG1u7uLq1haub29jemSFGQdcFHFxexLHDB3B09QAOLi8iBFYBX93AHloK'\
  b'wEitMNkZQK7yJP4+2F9O6H9pNIXkYa69n+wnENTHDMlvKU9YnzuDqLSFN9l7bATgYw89gl/7zd/HbDYrtzzrCF357Z/8od8az8oxwrlh1+V597JZcjtXd+Xq'\
  b'gRXMU06jfEWIINidR6xd2cBj59fw+IVLWLu6ge2dGVISJEkaVeSq06QLOHxgBXfdehK3njyG1QPLmHYdKOQ4gUTKRk4AWAA7nOC4k7J5aW+kIooG4srqXhNU'\
  b'7mOMrj0J7e00F6uOfYryvamey2fjIhyBgKUFv9LGodHYXXfcjjtv/QTue+jh/JEL8MBaOvS67/9nd//Kj735wfHMHAHnhlyPbE6PNztGQWdpcRFRGBTbTbO1'\
  b'M8Nj5y7gvk88hvNrVzCf9wABf/kFt+DVX3grXnjb0fLcv/ORs/jjB87hdz58FhevrOPBx87hrtOncMupYzh0cAWTEPZtqgwq8Ju7gKSmRfkHIQ+CbWpVcNO9'\
  b'JQMkeOFhoiroG4oP0T6/T7mSAIHIif4GjxNqtENs6Z/HOBK86PnPw30PPtJESvdfSi984w/8s7M//6NvHsvlI+DcWOt1P/Svnv/BK7Qn5yACVlZWKomrO3hr'\
  b'excPPfpJfOj+h7G1vQMA+MYvuRv/+7d9CVYWJ3ue/2V3nwAAPH55C//k5/8Yv/KHD2Ht8lU8+sRRPOeOW3H6puOYTDpt3KyyvLluXSoQ4Nsd2n4DUyUnl9dU'\
  b'5XAOl0gVwSXakTblqd3qVAAild71miaRi1qivoZAsi+PSPPcNOxQL4JGO07g0OGjuPOOW/HQI2c09AHWd4Uu78jLAPzueIZ+ftZYpfosrVOv/IaXXJmHUpmy'\
  b'tIMD4wXPuRvT6RQJhJiAze1d3PeJM/jw/Z/A1s4uOia8+x99Pb7tK56LaXf9r+jA4gSvedkduPOmVbzr/Y9gfX0L5y+soU/A8tIymDskUC0pa8Wn31PizvcR'\
  b'kFakqGph7HbkqlFSjU+uElEuiScqryOufN1Wtuz5qFTHAK1aoRLVfaqvGUXqc4n9R7Wah1q1Sq6kTsTYmc3w+NnHFbAyiG7Msfyqr/qaRz7y++8Zq1ZjhHPj'\
  b'rMd3Jiu1rJMjChAgSdAnwiwCIgm7sxkefPgM7nvwE9idzTAJjF/7h2/As28+9Od6vTe88i58wa1H8Pr/85exvbOLD37sPly8fBnPf86zsbq6mkllFwnU+GKg'\
  b'INYQoqQnyVW9GgmylKoYeC+BC1Dt15IcCfUCdAMWOqISQgYLEVKuhAmUuadsqpFjsyQgJohyWr4hVSxBI8Lp07cipveVOIqIsDsXPL4hXw7gv4xn6Rjh3BDr'\
  b'jT/8k0fPbE9vz8SCZ0nzZshXcsblqxt48JFH8dAjZzCbzwEC3v7mv4YX3n7003rdYwcXcWhlEf/tw49BRLB+dQNPrF3E4tIypgtLSGAIUdWwJI1aNLqJyBFO'\
  b'EqCPNXqIIjXaSFXkl0AlSksWiUh2E7QIKN+WVEdEqniWouXJep6kvyfElEAC9Cnm21NCEskCw5QgSVSPY3+LGfskIaaohLNkYI8RW5ubuHLlCggFh7Ddy/TS'\
  b'B//Lx8YzdQScG2I968tff+f53e4YWs6zsKzr6xs498QFnD13HmuXLiGlXC/6hi95Fr7nVc//C732S+48jl9//6O4cGUbxEA/j7i4dglLS8tYWFwEwIiSEHXj'\
  b'2sYX6OZOuaEUpiJOsWppbPPDNn9CirEBg/zYlKtqKeafJSlAJP1PXyNlkIAICAoeKYNQfa78uAws9XlSjPk1y+2pgE2K+f7MjLW1i7hw8UJpVCVkAP3Sr/ya'\
  b'xQf+8L3nxrN1TKme9msn0ZEGbJrSLyGlhO3t7VLezvoZwlve+PLPyOu/6Q0vxd+65ze0siSY7e7ivvvuAzPj0OFD6EKnqUtyqU1bGqdBl7aIaGqVb/S3F1Kc'\
  b'2tTKbmdt3RgS1ZX4jTm1cjak9fNqPFKRJOprDF5IXzjFNuU7eOAgpl2H+XzuzT5weVtOjmfqCDg3xIpCk71AoztDS8dVpJc5jNuPH8Cpw8ufkdf/yhffComp'\
  b'aFdIgM2tLTz40IO4+667sHpwFRzUeFTLTgTS6pFUBbGxIuU4XaUqJVDTZp7r7VZZSimpCpqQYKIjKlxLkuTU0UmfwpglKtHKEHRE0yUzjjcVtn8OFNYGWFxc'\
  b'QB/74mqYsUlwYQuL45k6As4NsTbnvLw3ncqUJxOQYmpaEyQl/NUX3/oZPYbn33oEH33sUpmmEIiwvr6OB+5/AKdP34zDR45iOpk44V6Gl8oLS8VHGVpmWDPW'\
  b'4Ea9LQ5AIm92BQSx1Ib0E6nILMXcXQEJRgx7tU4NaGpcVIntFEUJ+gyGOzs7LtIkUMpAPEuC133/P7nrV37sLQ+NZ+wIODfAco1GRbnrFGqD/OPogc/sBffl'\
  b'zz6Jj5y5iJgIzFyiia3tLTz8yCPYWN/ATSdvwtLSko6H4ZqKKAhSMaaRAoz5b5r+SDVHh0u/ynPo4yzdMj0NGhP3vfOvkgOfYg5fUjo7pJaIL77MLmITJFy6'\
  b'dKkAnkV0Bn7ziNXxPB0B58YAm329fJ2gzviQ6/mG/gXW4ZVpNtdCrfAE425ixMW1i9jZ3cGpU6ewvLwEJnblb1UcRxfRWKk8tQAqjntB2ditdVhOy/azE8vR'\
  b'UBvtoFaa9BafdgEAc9bhWG+IGZclB2j2HtbX1/X59DUof/4pCWZRVsZzdQScp/2aJ+KaRDm8gQOZxlqPPuOQ8wcfPwukBGHOvUciZeNxZqqxvb2FRx95BAdX'\
  b'V3Ho0CFMpxOEkAuXXPInN36GnPaFMhfSQoiUkTXkWzGNv3FEsIgDM/jeVjd4OGWqmagFohTrlAn7NzpQzJxRfpbtne2BMoGU/yFc2ZExwhkB5+m/Ls26gH3M'\
  b'xAtPQez2cj75/+j+z2yF9tzaJg4tL2BjZ57LyQiO2zCOhtDHHlcuX8LVq1exsryEpaVlLC4uoOsmCEwgZkiKOeUaGCnXqlMV91Xf4+SkfDmWoaErsTgxn/jP'\
  b'SSMp1M+MiWvkYymeAllN7ayDMz92Nu+xs70N60dnTbNqyjueqyPg3EBZVeZojHtgjfIFzFJ8hiXXenNE8hlaZ9c2sba+Aw68J87KgKdsMPvULmFzYxMbmxsI'\
  b'HDCdTrEwnaKbdAghgIkRAoOJ9XlrxCZqZgpNj1o/ZtGN7lMsaqpKno+hhtOR0paQKCdfknItjV1XuZgkWt8HEWE2n+P+j99X0izxICPm5zMizgg4NwraUN2Q'\
  b'9aqdtDKFGv1o0BCT4Jfe9yDe8Mq7/8Kv/jO//VEsLXSY9bEx5BIBmK0dodaHLNspI2RSws7ODnZUK8TMuaqmIUgmoalwUZn/qVW3EBhdl4HKUiwmLu0F4krm'\
  b'9TOQIhFooh3BHn6HkN0CrVWEkK07UkpIMWFrawuPPXYGuzu7iq1q/iECklymp6ayNq4RcJ7Gq5rTVcMYAkBMIGKw6lfMvU/UBvRHfvHevzDgrG/P8K/e+X7c'\
  b'edMqzl3tQSxgZgQCAkMBJKc/gUnbDQx/ZM/kTIggxgjENlIgoQogkqrzoD4ZgcGBckSkDoUFpMqz58d13USjKAVjrulbrrpXH6A9gj8IEgT9bIbNjS2sb1zF'\
  b'+tV1pBSdb48S9crv+F75cY2Ac+OkVCCEYgGar7Js+hvmwkXElKOKYwcX8U9+/o/wlje+4tN+2b/91vfg1NEVTDrGrI/KazgQIRsbUy1CyXl/FjiQDJAppcKf'\
  b'iBMu5obNSsBabxOYIDGBKCFGw93Kxxgw5TRK9lTuSFPPpHwNc246Za6iQWIgsEVQwO7uLq5evYrtnW0kfVEiatKpodPhGNuMgHNDoY1tDAAIIW/YjgnMeZMw'\
  b'U25UBMAISERYnAY88Phl/Iff/hi+/S8/78/9qv/LPe/FRx65iBffeRwPn7+K1GeCOnBOc0LgvIFRQcfPFa/EsHsPxqkQZS7Gdq54n0KNRvTh5AyWa0eHuHSp'\
  b'ApekBJAgmYsW+lIJS1INdexvFlWZMBAi6Oc9YumzQh3CBzRRWCafqYAZxijnc754/Ag+GykVKacR0IWAQMC0Y0y6gGlgTCcBk46xOOmw0HXoOsIkMM5f2caz'\
  b'Th7GO//4E3jzT/82NnfmT5okfu0/fgd++0Nn8PLnnEQfBZe3ZhAGAhO4C2AOYGYwB3QhpzmceWMEGkQQUrcra8N7ViyLK+4Uaz+lhPdqj8hSKQYYXAljeMf3'\
  b'rNGp2qT6VFz+Jo1eKaUIiQmx79H3fSatNZphpqZHLVexpHBVJlgU/Xdcn9s1dot/FtaxV37j81jJ1UBApyTqpGNMuw6TLiAQo+sYIRA63dV9jOgC45XPOYUz'\
  b'Fzfwf/zsH2A2j7j9xCoOLk33BZqf+vU/w/fd8x5ACH/pBbdh0gV87LGL2NyJCEyYdAGTENB1nI8jZA4puEiLCAjExW/YdDpVMlR1M3aT9SS1ZXK0iOFvUaWw'\
  b'SOssWKt19nzmNOiaHsiV4IcjIoA9zZxW9arPqdUwjXhEgXR5inj2/b9x/3jGjinV0ztsVKI0MMomn3YdusAIIfMUICsXC2JidJz9Xx56/AqWFyb4ortvwrNO'\
  b'reI3/+xR/Jv3fgjHDk6xsrAAKxU/dnEDm7tzrEw7fPFzb8aL77gJlzd3cO8D57CxM8ekIxDnCKsLIVeZmBGIQUErRBIslNByM4HYyFopXRlVUZQaDy4jxmtj'\
  b'5MDd3JWhywxxRS1rwKwPqLajvkolACjlY+RkUZY0TeROz1wFimU+uf5dLTCMqE9p5HFGwLlhACeTw5OO838hp1AdBzATOtWx2GaNMSIFQozZjOqjZ9awM4t4'\
  b'8Z3H8cYvfx4ub+7ikfNXcXYtgwwR8OK7TuLmIys4eeQgQiA8cHYNH374Iq5u7WKSy1E5nQsBgfPvXFIpdpFHyoZgIW+/JFx4kChcFMq1uoOSkgCivI3sGf3i'\
  b'zIwda+tBSkFCuRlTNkvT7FlyK0cwkx9RlVsaNDdiEKI4JBlMjhBkgtzAZ4ScEXBujA81MLpA6Lqg0U3AdBoQkFMoq1QZ6ETOJGjiiF4IKSZ84txlXN7Ywe0n'\
  b'VnH62Cqed9txvPDOk5lgTYIYBduzHmcvruPh81dwdu0qdmZ9Joc1XSNmBbhQeqWIuJqQEwAJCKrqlZR07rdWsEQgKU9KIOHstkcoat1K0A5mv/g0Jjl+R8Gm'\
  b'9ooPAMFbUlgTps5/ERnMCNxnnAyKNYZVzsi1OajhuzscGTmcEXBuiAgn5A0/CYyFScjpFAOdVos6jTiobMBsWRGZEVLMABQFl7Z2cPWRXTxw9hJWlqZYmk7Q'\
  b'dbmMvj2bY2NrF5s7M8zmEQLBdNohMOf/dDBeIBXtaZrXePFonBJFIFEggRGiTvhMmt4EymOIJUEoO/UJOG9kAxSNMIhrS0KpVPmpFcVu2BI1TXkIQHIGX5kZ'\
  b'Rm49TQ5/pCiZi4ZJG0Mb3x5yHA4IzEBK0oLMvqM7xzUCztNwTZhLZNNxyNEO14gnl8epXK4TEsABPRMoAl1ipJAQU/YH3prNsbEzr/lK4UvyVXvS1bQpqNLX'\
  b'gC9wrpYZ0FTqJcuOJQk6EaSgtp4sSInzfCjzGmYBRXXtI0ZKqtgVQbLOcng+J/mwpVSLSluFN9ppUrSal8l15sL4eVXDqKiCUvXzsde1qpRNL6axtWEEnBsi'\
  b'wmFC4BxRdKp/mSh5nCtUyuNo9SVGQgq5hN0FRh8j+kQIidVEPG/yvMESkuQqDlMemUBceaN8RVfNDbJ4L2to0HRsmzBOgiqNNf1gocwlp4SYGCnaLO/8c6QE'\
  b'YskBCLTqU8ZeAtYpburjoTq4RChAO3HB6YFESetKCRV4hVCqt5WIRu+YxPHUuRKVQLWfU+rfJQ17+cc1As7TdIVQS9CBs9uegVAg0hK1gUBOsZIIIgF9yi0H'\
  b'XYx5zlKKiClvjZwWhBpJKIawpg3U9DkhRz2mfNF2AXLmeVE3Z1LeRlThKwIkyvqZRDm74QTMleztI2mAYkI6JYCLc3EqkVhJkYqoWK6ZyZBrBBVN18ogPqrt'\
  b'INVbWSOjVAlkIRu0xwqCuX8toaZYInvraeMaAefpG+HYf8rZFLApmhcgqDI3F15YJzcwwNkgi7lDjAmBqYxQMeVt1aZk9AjQHq3gerZQm0RNXVy2tHWt6+Zj'\
  b'Hd+SOEcWFkFB8jzyXlOTLhD6mMv9cJwJIWma44zQC8vbsMJ1BqgNAvcVrHrHYkVqZXFRsMyRmCpqSGwsKHwfGHme2pvBizS+O8XRcFwj4Dytl2pNvKak6FQ0'\
  b'lUnIG73eXXusdCP0URCC6kVEwMJ7qjo2u9tey9S11rXNRK0DHqQAogUaxKZJqa0KJIIIQUd5UF1gIz+otAbUAnWyGTJoISODAWnYYQlM65qc9n50WmWyz6Q2'\
  b'wNbqVJkCUeabc42MTHtjx1NavwjCACVpzOvHNQLODYE3hd8Qs07IvVN5azB6kWKkXrqeXSXFaJHaZJkjj0ZAJxVkcqOj93gRtRhF1b6UQEP1toIySsbGbBZT'\
  b'dXt+J8LLTQkuLSvNmEkdAN3kS6DaR2j1ati6IDIoFvkh43ZY0joClkEzZVpnjW5s9hS0+z0HP2YFhuJ4SCRIYz41As6NsmxiZEyMQFr9oYi+Z4Dz7OtQuqON'\
  b'DK2Nh7Y5rOKShXcYCEc0akgZjIyLUZQosOc3tGEDdMOZD3ABIZ2gWfQyyTidVPUr5YdUvHNKuqRWEqUj3EBAwaYqlBX8xGIkccQwGo2NIVHj1udUzfb5sL41'\
  b'9l6DNivL4ipiRImZRBdx3svjGgHnabwkATElhJSNolhTA2aBJAKnTBAXzqdoR9oJBgnJZxRlg5lHcOtPg8bqoepd3OOwT/nZ9x7YSF7UTm0ps6CyFqcCqk7P'\
  b'tIhF3LOL1PEwg0iprZShdn1XCqikiSIu4iv/Out2F13BB2pkYKOgm6QcHzn/IaFR+jcCzg2w+tiDEDAHQAGYQTAJrIQnAZS0A5saywRGrf6YK3DpyZZUApfC'\
  b'x1KrviXnFm7+xTRgVmxfl2hIvPNwLjsX4IOVj7MjYUqCPtq431w1y+BTVcM0aGvwRunVyN2bafljrhCYJJO8lcepZLQU4HJWreKyyZTQDImgSmf7GeNjhDMC'\
  b'zg2xlsNsO1AGmAkYnAgQLp3ZSvEiaWCTyE++zNdqobZwu3f6gwxgxFKjynRQDqia1qZ94rE6PwsQcZFByimUZMDBQi80jTEhKsgkFQaKM0FvxcUpe+y4Pqhh'\
  b'V3cbeVUIYJtkMSCBPUz5VFRKWpa5LAPK7NPD2dbUQE/TXBlpnBFwboT12//0O99zo72n1/2Df/fSrRhuF4ECTSbBbWqMGXOZKLi2iiWgqTbJQAsjLuXziuXK'\
  b'F1krRhmIBzh1c9XXJKljaqTMIJRmQkYBfB7C+LhGwBnXU2bF1NN8Xs3+UoquPcrrYciymuLdXJswa1TVjOYa6mNc17iROOY8SJTLT8lpdwzcssBPgc/GKgNl'\
  b'ImeWFiQ3AXUEnBFwxvXUBJw+0rxXFW8hiam2RJEflJed/owQbmtWLq2SgW37HvscqR43bpBdroTBK4aLqJmFMhgR5QqejSQujaXZ4J0g4LFffASccT01Vx8j'\
  b'5r0K9lxFDD5NQbaEYDavYufmh9rkaaVxz0GR85+ghsiuIMTaekFuYinBer2oLZfreBiSDIoMQcpt4+U1ZYxwRsAZ11NzSYo0m1dzeBpMoyDKVStvI+F9sJIk'\
  b'bbnQFKmV2bT/OgAq0qJWHahAZOLGKu6rcgFpxI5JzLgrK3b2mYozrhFwxvVUWZ6zoUSuE1z1NFE70yHIPQSV9DVDdK82LnodajU4VTEMTXtyCiWecNaoygNc'\
  b'0R8laXxzYJMkUkW30vE1Bjgj4Hw21ze95a1Ls4ibBcBWz6fsRC82Lb4GLV5s5+7XTBEYiD0ENhpGsk1VvbWo9ilBhPcZxebKwl5E57U2KbdDiPYuFfFbOexB'\
  b'HzR5MRwaOwg3BFxLTO44RKvr5blJNudyyMba5CmcNlINgl7+AAAgAElEQVQmIEUFEitPsbjXp9qG0UQnltJI07xZdTJ2EChCPnHvyS8DvhzuEChRHTGsTaA+'\
  b'HRNYqf7aHM67/tO33T3lzVP1RfYEYbXtg9E4EtaPsn7g/rZ5XLwk4N2v+daffWgEnBtovf7NP3H35pxOXZ6HQ5dnmPzB+b2XtKL/sBDeTnagdFPDWa7Q0LN3'\
  b'8Fy16dD2a4tcxilA/PxHakatSGloQumarhUbqC6FVDho87sHBWbZL1+hdoMO7kO+/8AplbWhPScuTNnXwuwwSJ3/gDL0L2td1OCK62daX0k/J2ms02v6VFSN'\
  b'XilDrvs8K4x5MOwOZXa7WmZodatWpzyiDYikwZqErVPHlh84Xsrs/ruQNirz5bKBfEotTxNYzc70Oz4BAH/6K89/8U5/pN+er67P4soTX/2t7/joCDhPO5C5'\
  b'5+61Xbrrk1th5d6LTfw+CNvrqc4AuGMcXJwWD9xSU7F51yQDtwVpbbitJ4mqr4tdYS2IoLarAH4mgm9O9C0LBXS8/YKFIE20heZRtom9PcPeWEr2XpCpVnNi'\
  b'7AtwMVNpLcizpGqbABEae1ApSl5UTY1Boujnsie68w2ZVKpLvimz2EuoR499PyKpeOGgfNVUJzyQRmWp4LVmfdehcMQfCfamfYMG1qS8FOtEVS7ug0AI6mHE'\
  b'ZQRh6U9b7i53i+HSEYIc+cNfePmzN2cnL27Oj7z/tX/zbdsj4DxVU6UffuvS5pyf9/BGd9u9F93l2/QXROBA6Dg70U0nHW4/fgAnDi3jyMoCThxaxuI04MSh'\
  b'ZRxcnBQTqqaIIfuckTWmL31HIN9XRK4vSooKuPYrYU+vEABEAzQh11xZ75ukwTgkFFVwkcN4/17/eqZLsbJ2dL1Sfh5lSsDObI7NrW3sznr0scfG1g42Njax'\
  b'sbmFze2tWvHRvoIkqdib1kjOWX7CUzxu9q7UrI4sfdoDoGjQmoufsi+lk2WAuXeNCIn1AyuWIToL3b63ay3XegJirBx5GRYPPBuSZo3FRq3UOWczYRCl4XXA'\
  b'pVUJs60z6HcvYnfzUbAwIBEL3SYvhAdPrC50X/Ub//GvXXjVt77z90bAeYqtV/3AT/4P917sjs3iXtaRSLC0EHDq8AqedeowXnTHUZw+egArCxMsTAIOrSzo'\
  b'SJVrgcpejqVMf5SaCqV9NrdNqfU2CBZR28aHAxBLjZIKW6wvMqYWVApQSQWsJC14iShoSX68KMgVwHLPG2UvmJlrxXIEDukbi5JJ2p3dGXZ2tnHu3Hk8+sjD'\
  b'2Nra0q51AYV6Vffq4hpReh8a16BBTYM7fM9Xw4EN/ZJzu7szVk+wynfpTyNC0rlbxuOY9einmttQbEIkYWHpNFZPfBkk7Q5waUA+S5//SgGQ6HPpEoKJRKR+'\
  b'EynuYL5zDrsbD2Hn6sexu3UegoSO53Rs8f4Tv//2V752bffOe7/ub/7sJ0fAeQqkTvdfnbzw41c8uYHS5Bc6xslDK/iS557C17zsDpw6stLMMxoOaLsezJiP'\
  b'Sv7Zog6qPYoOWGSw8RsgQAWaAjLiNj7qc8dkz1sjnOjeZizRSn2dFngyaxFT3hTRQEeqT0wUacDLxnunlJs1Kw2dcu+RCEJgrCwv4447bsfhQ6v4sz/9U2xt'\
  b'bzfdC5lETWprqp+cevtUjtuNrzORDerUB2B4/XBD7Nysl0yi19SpzrVqu+aLUrmYjKW2nX4ftCHH+4r0CN0CgIUmv7peA2j+1H1vWa9bLgLTg4AAiwfuwNLq'\
  b'8zA7/EJsXPgTbF35CPrdqxAQlrpL4XRYe8Wvv+0ND3/N3/ilD4yA83mpNP3E0hO7/Mp717pDRWevJzFrqH1oZQHPv+0ovu7ld+H5tx5FYBrGtHtQZXCeN9GF'\
  b'OHBJDTAMIpnBaKbkoxqXQmkZq0QX5oEVS61JR7e4502pjWYsgkkevPTYo86SKscAQBLp6+coyjq+o+jUTX2C4n9TBHLa6Kg5lxmKEYClxQUcOXoYu2d36oVc'\
  b'Tdn9mBiLAqx/k/dNlepscBHXUKoAzI7wzSboWg1TrEqp5aXED+gUNxrH8U/XHS5OLZ/W714sIQ9Z5CIx38qdi2YG55H0AHXlvoQISN8YuHeTRXSrz8HigTuw'\
  b'/sQtuHr+fdjdPFNqjieWPnTHb/yHrz78qm9/z2+NgPM5XF//Qz9xy33r3UvXdjlUo5WKJYEDbj6ygr/yolvw6pfchiMHFvek6c4vqmz8NCBzfSQSU8uJ6EDc'\
  b'vGkLWEl5TAGm5MBikFL1etgxudcSB0CS+ZuI6pHj71uiHXf8EZWqKNFPaqOsqDYSBiZZW1NRMKa+RBlmACaKxkmfzKpu9tilxaWs5DUzMZdSNZbBjEbAZ+pi'\
  b'0+j45KlyMUaItU2dVopGaptGjdPJeh8qnsfEVF6b1J50z0DyfdGidsHH+VVIXAfzggJln+dhEbIICdFZYQxydB0bQc1ki16/W89LAYdOvBILyzfjiQffgZ3N'\
  b'x4q26MjCg4fe87bXfPlX/413/e4IOJ+jEvefXZq8aBZpn3g7X8FOH1vGG7/0WfiyLziNhUloCNbkJhSkwWkWXVpBjlwFOcARmwhATRqTo4x64kQ1ITfaMw7T'\
  b'pn1SrQIiBlLQsm4iB1pUIhkf/UTH9EZ9zr50VqdSNk4pFiBJKWmlJxVgQYlk7D6xzIPK+hspplwxRX3OhPlsPhAsobQcWE8VGp0TNbOnSBstfU+Wb39gnWjh'\
  b'q0TewrXpx/Kd4Pqa2bPZEbsu1frUHur1RIlxExI3IWw8DYproU+h2pKWAZOAKDhZjpSIyKdtBk6LKydw/M7X4twDP4fZ9lo5t04sfvz4u//961/ytd/xyx8Y'\
  b'AeezuF7zpnteeu/Fye37hb12Yhw9uIRv/vLn4kufdxqBCbPYkn9DYrcAiOwNvz0f05C07vbKsQyfnzS6IU2BqmtdLzV1kgF3Yq8fXRRUoi2hmlLpxblXMKqp'\
  b'XSoOfDWCqYCS/V+Spk8oEQ6sQiUp80UFdFLxZJaYlFeKlbRNgp3dbaxdXiuzoUqbg7Y6WAWLmg1ITVRaq0dOPGkSAqn6HmsEtcF2RHDGxcoZDeTKxX7UxJKp'\
  b'iKNUZkDXj3B8mj3fROrXEULnBAppcBrGWrUsT8I5mknzPZGPH1UMoAod0y4WV07gxB1fi3MP/BL6+WapLJ5Y/OAdv/Zv37j+dd/18w+OgPNZApsPXprevl/0'\
  b'a8H5JAS86gtvw4vuuAlRidIhkHjAaYhWR3YmizKo8jr+th4lwm+qOUMeJ3oyOVEGGjVBLxEKclk3+ihowM/YcyUXPUlKLq2pqRG0X2kYqYh7jP3swQhwgCQO'\
  b'bEQjGg9aqh1JkjCfz3DhwkVsbW2pABBFhexGQtSpElR1O2xaFH1fTFICBYbTubhIJqBWwMRN2Cxd6ZQN5LWK3YwczkZg2sxpgoVPFd5QlTbkx8yQ4gYkLelr'\
  b'9QUxCydUDthTyfNs+4joTtpYSekS2UQFyEryLR84hSOnXoEnHv2vDfqdWPzwi37pp7/t7Bu++2e2R8D5DFeicmTTlqRRiMD8+50nV/Hy55wGTzrM495S7x5g'\
  b'qFzknpSncCADIlYGVaA0SNeiXvAspfG6luqK56tR+nOiotEpqZIjZy01QrH6jGBkz2SfDqWUVH6SGtBoIhz3twJQ0gKNpU3G0diVNQMVQSRid2cXl69cxvb2'\
  b'FgJzKVlXoZ8N4xPV1Wj8YuppVkWwzqsjZk2npIyBadyWpVgj5iqk5r5mkxpUeZ0oVX1Pzvnyj1RNj60Py8ZifSoOJ4v5CKnfRT9bw3TxcBYSIZboqgCQ1Miu'\
  b'hi2sKVjKEZA3JCu4FiHoAOlrmiY9IAkHDt2JK08cw+7mhQKCgXZwaPLoVwD49RFwPoME8Z9d6l7UqGKbcnb+fWHa4YuefRqrB5Yxj9REMiXtcRFLUyly3MkQ'\
  b'mJIT1RlQ9Y4Q7t2xJCOJHRhFfwwuLfO6mj5VgLCog5pUKJWSb5Jamq6ErwnoHAGMPEjPUisjflOMbkZW0jFSolGSlMgFFg0lDAAop2M7s11srK9jPp+59Mm8'\
  b'halUorKC26Z95ohC8aQojSnkO4ZiLbGnQcPRQpmbSSnl75MErD1TFtWElFsYkrUfsGu1QFVLJ3kS3lskhWcSEBDnmG+dAw7cqoDRa8Wq10Nmpz42KXPvgCWD'\
  b'ddOHV9K2eS6hy0xL51zOz24yxeHjX4ALO7+PPs5zdQ6Cg92ZxV/9t99492u/6xceHAHnM1D6vm+9e+ks6qlZ2gZkj5bm+Ooybjt5BD0Yca7gMtCt+IqTB5zo'\
  b'oqDo1L5zlxJVLoV04+YxuAJxIENFnSuQlpPR+ySrqKQ6q8oI3Ko/kZZ3KZMTYi5zp+jSoQpUMkh7UnIlbJ92wf9tmEqp4icltRA1oBPE1CP2EbPZDnZ2dkvZ'\
  b'u0701KZJyamVrzxlaT+jTN9VcNBkS72LNSpCTU3sSm9zpkQjF2YGk06X4MyV2WROISuhMwSxKLstJ5ZSlrdu8U9hwCWkkoocb812zkHSVRB3ehWJJeogBCdo'\
  b'TCDqtHqladUekAva4ps0oplVgBVjmLL+59DRu3Hp3AcgKSIFgejV7Oj0vhcAGAHnL7ou7PAr13Y4NGIN6+Mxd3ANvY+urmB5aQk7kZoqlAxLxNICzFBwZ6lD'\
  b'NDPzROX+Nb2iGrkIZfI3DatOlCcceH5E75AKaZv0GJRTMX2M3mYHGe2EtbI0PIB4UIkOVLKuJokniPeCi+ltPAB68jnGHv28x7yfI8YEkZgBJdvqgYgLv8Gq'\
  b'rNFR5giOg8lAogSvxjBmbWFD9bhabNXpDG7Ti0ZQUDGjCJfucUoJCIKYyJmq63Mh1qmfioSiudT1NH+1CNq6FM62LwD9JhAWNGbJKidCyKBi8RsRINsa5VSN'\
  b'TsbOqMg717/1Oi8+qgjS1Jex3i4Jh47ehYuP/1lOJ5khKWJK6/yuf/u6l7zmu37lAyPg/MV4m0Ml3jRJOLW5kF1Bp5MJiDvs9m1FKUor4PPCPNmXAKZS5o4K'\
  b'bNG1ISRpRXx9KdZoquPEOqWM7HkWESSLVBx3YuNfJKV26Jz9rroZkQQSQhQX3TSplBQQSiUtcoI95DQqpegI5By95H8jUopIMeX/DIzMl4aCAwOGccPE1XaG'\
  b'3Khi/x1ZYychN8my9VM5zsNSsqF7RlMr17Qsv38qamoBIZAbFlgeGyDIQFkqW8U05LoqHEfeZtgJzJjP1jHbeQKLyydKh3zhniRqZDMBYg+dUAhEMssSBe1q'\
  b'DtY23jIgM+W7KAOSXXwArKwex9r5DpJmjicDDnUP3Q5gBJxPd91/tXvh3jOhNULx7H43nWA3MeZ9K6arZK1K9a3S01ShqIlgkrQcTqwFA50HrkSkRhOkQkCJ'\
  b'SSOUzFckiyR8CmTlaq9zsWZOx9nk44oDTUxqoiXxvIpVoUx8KDGH3Ob5q4BoHExULidKROr7wtdAR6fUdoBcAWpHcJPO00IhfKvlhehAPwWb4MR2bFYaOUIi'\
  b'zkSvBjRqtl6rWaIbqkQjjk/LXw5DyNz+OGufWMAppz5J3w8p6ACp2FV4pOHrFqk8o1SraZtXHsbC4gqYJnXul1ce07yMTUaykngmj8miIB2fI0Ympb6WJRG0'\
  b'hG7AFADpMe0W0YUOu/3MT/XBYrdJ7/rp177kNd/9qx8YAefPub7qB+/54o9cJqq2J9U9hYqrGzdk4oHlZewkQugr3xIHupmoAxpNPGfAwsrXJK+TcVFLIVil'\
  b'Ao2Uf2P53aT+Vs3JaYmCglPqmtWmgY3A8y1D7sYrgROi+jH4VApJasSiEU1SUMsRSwaZPkY7uJLKEdXKUb6mcmkVANWeKKBtrCw8BPv55koIa7VJFHxYNTbB'\
  b'cz2UE7BCMMPuV6eHUtEEcwbDYnCVwNqFbvYUErTNIUm5kDBL4cwsMDZQS9a7ld/DdaZ1OUGf+vskSdi8eharh2/FdHogp2zUA9Ll46cEpA6gCLJKVmbxHdCF'\
  b'KooEIGkOSuwuoVbR0jeeZiAkpDTHdGEF/XxH5RZ9icQOTx5+2kQ5TxnA+aa3vHXpT9a6m8nrMEr+7oDHZdaBGQtLy9iOBIpowMSXxHPUQ+iTUXoofUWQKpSL'\
  b'GvImddRLJpaTqnshWMqUN25U3qSWkquqFwUEYkPqQihHSKhzyDEQ2yXP7xTeJQOIAYkB2x6hn/FENgnXlYZzv1K1AC0G6IMpnfV7oKZkTc4uiws3g+JXTBr1'\
  b'ZJBRrkTBh9V6lIg0BdPqDvvZn75cTa6YIxDhApjMSccN59eMSkP31AORStc4SJoJExBvJXF9Ekc0vCUOZaifpDm2Nx7F5PDp/N6kA9CrgVtQsJFWCIj891xO'\
  b'75UgZi2Ta1QjUhTM+WdLBZHTs9QDqS+q6YLfAkz5Kv3q//sNd7/2e/7zgyPgPMl1acYvm0favzppbidUzbMtudrYnmNzN1dJpBHekaZBVFKSJKkMVhKJ1ffG'\
  b'q3UFJRWKPm3SUrKlSqLTJy36SDEVDqSPc8Q+IsYcZVTAqZUlqxhZGsUARG0UrEwNSY1Yz0CLSO9HaK/ChgtGstqkTzdWl7wK1vQxpcnSNDUZYANRQ6l4iYk1'\
  b'Q3aBne9QNeiyCIatXKxRhmVdxdzMyueEfQBPgEAl6kzayp4vLApUJEgxE6kJgi516LlXuQs5u1Iz+aLSf3U9HQ4XT1GbC88l6t5cv4CVA4fRdRO9d5/BJu2U'\
  b'z27PG0oabdEMSMHUifkzF21+Tb1r6DPDsgCkrYxNyM6BKUYwcTmvAeBAd/7ZT4eK1VMGcD6x0R33M6Ybb5Tm6quplWQgOfP4RRw4fAzTxaUiuxegIUwNTKKJ'\
  b'4+BEdY4fyVGCFHCSlMEnpYSYIubzHv18htl8jhh7pChIsdcJlNF1WkspbVp0ZMSuvQceOsk5oX2xsnT1Gr/ZmawktAeZXUJAtXLkwaZc4X3TkrRPQdXViskq'\
  b'Osq/uNexjmsSLkDCpHOfLLJxvA9KapN5HNZQlj1XTPtVi4AABqcECQxKAlZrDUoAgoAs0GRB0NePMWGo45LGnuT63eIN0CjXREzo59vY3byE7sCKsuYaSgq7'\
  b'3qqo53KnFamcIuWPr4ekub6/rlh0kGqRyBTjslsEYZJ6SMw6HmZGTEmtVDMHdzCcWfylf/PXl97wP/2n7RFwPsV6zZvueckH18h590pjSu17cerVI8c5T1y8'\
  b'gJsun8SRI9WyvKRAvtNZrzKi1aLc8Z1KJDFsTOz7iL7vMZ/PMJ/NsL2zg9j36PvYdFz7sbPGRRRX7TYzcJ7B1JKWWgq1qQf2fkPTeMglrCfTvQyMScvkSqlR'\
  b'RuNwJZWYseZI8UI8ptpaAM+roPRHZT6NNA2ylMilSb7srbSQWWsGVLtP6LgZ9nkUSdNvRMq5WeqJEErjqZDxerkKEO25EgMcgUTZf1kJf5hDIOq0zye72KI4'\
  b'hXGmgPX185hMjmJhYQEkqhIm73gfQBTzFpNOuR7H65htAXqn4q72tlY8sGNNcY6UejDlSmUwW1PinK4BmNLGc5/qXM5TAnCe2ObTpVvYTkoZ1qjEGTzVjbuz'\
  b's4OLFy9gcWkRIQQtm0rhRpjaniPSFCuhqvMq0dpjPptjZzbDfHcXs/kMse8RY2qirpoOSFGSlejE+nW0RCy2+e1O7pxkavtwmp4/fX6z7MyeuFxbIJlK2C0g'\
  b'MFXDLPaku7dLoBphZdK4gXI4uUx1TWQpCYYrQuVIxhmeswEGU+2fAmCHDwUhAyUUcKtK5dY4PUcprF8TWysDESSQE0USQAzipFIHUV2QlDSQ/IgaoSc1jiq/'\
  b'potuiMHERdMTY8T6+joYwGTaRrEGAjnSnUEwq9Wsxhi+nns+Za5hunJ5IujnfZUMMiOV6C1zR5IiFsPayTGl+hTrG9/y1qN/+ARPyOsnhibmBYdq9GBNgiIJ'\
  b'55+4gNWDq1heWS7+NBDvtyuI1iynGpXov+iU0M977OxsY2d3B/N5r71J4rqgpeUvfPTluKUasOSNmvlZgpdIl55qthhf1S11IJO2CFBR3FKQwtVYpa6GT+Ie'\
  b'p5GIWIm5etU0Em17T0JNqkeOADZ9jIEniQGFVBsIBSk/EC/Ag4zqccSEgVT5IL0vN+khlblUSFVpnKuK+b2nJLV0oAgmMSuQhSnva04ZeNRgHSxIqZ0O8WTm'\
  b'UjHXMoVAEChoWsvo5z3WNzawvLSMyXSi74uRoc9FsdrtXi1GU+lkL/O5iquAuBI5FRlF3/dqzh6K0DMwIaqcnplzWvVTf33pDd/71E2rPu+AszHjZ9nJKW5S'\
  b'Yukn9vuK3HAogW48wtbWJh597AxOn7oZ04XpoCGxcjNFjFfmY2deZz6bY3d3G7PZXEN22rd641u5gk0xME1EwQCt/EjYo2alkproVEgvfLPXIaUFZED22nOw'\
  b's3lA5VcG1EMumCiYlM/NaWbqr9pSMHiHZohXJ8dwBRdz8FOwM54m8zEC+DStfJYmBMx2EQQPPvU+zScdjG9TwlVyikkMBQ/93BMQqU6PMPWySL5vLhZlgJfo'\
  b'fZWvHenU+WOEEFgJ71AuNgKgY0I/77GZNrEUF9F1XZ7QoPBElBty6/eV2simpONVQU7F9KtykPP5HFtbW7Aaq02FSEkKiSxEYKanfFr1eQectV0+7t37DHSI'\
  b'/DwjakJ5ccSmnRdXLl9Gij2OHz+Bhem0XD1M+yKunyGpfN84mn42R9TxJXZFrtvT0g8us6OCzZvyEULpHFbkYXFRGelmy1UJphrFGBiwjyrINy/W6Kg0SnoQ'\
  b'9g2Pg3FTBF9uJngfXiG46Qhsk8Eb5tb/zSpYbAI/1LSL2fQ0RQ1YIx2m0rDpoza2yZ36fOTJLmkZ46Tq7KifZ0w2jiWB1CeMmcBJuT0WSFSeKbbcFrlo8HqJ'\
  b'VaA4Ye2EbyYZ2hQQtnaOXP6fzedIIuhE0HHIQ/5UI5X5N++7rK3qqZ6PJgjMfkq1LSbGiM3NTfTzeaEVbBQNMUNSQlASGQCmfPX4mFJdZ53b5klbJfFX/Ta9'\
  b'sipl2bC2jQKDQdja3MInZ5/EyvIyFheX0HXsBG5VwWtg08ceEjWi0ZO9Xhk9mVEH1TUXx0I7uOjE09xG7Op9mEKJMDLYcNmklW/w1SiXXsngPV+jqlvmQ8lw'\
  b'7pK0gDhkbwSDKlYFFnFG5OT6nsiTyrmur8R5G7WYVQXr45kcsUz+fVPDY9mVvk9Zg5NbLRjghJhERYBSIjFmQorkSvOWmuqkhrin7j6/1nm50G0tZ/Mvzt8j'\
  b'5yjHwCYLG1mrWNqHF7P9hHQTBAtTUZtuy/tqohsq4s7aHpNBad732NneQT+fuW53qg4XSXJ5XGqZfLm7uDwCzjXW6//+W+++92K7IUz9KkWNWjczOwsEn/Z4'\
  b'/iClhK3NTezs7GLSBVDgQt4S6hWlyPC1GYidLQIxmnRJbPOV8TOpnrXiHuN5HqvyeCWtcRz62lyej5qycsPzUMl8FHikmE0RV80HOQkB9tDBXqndksXmLVxL'\
  b'ZuZFrGV1qemsfUNcKl1U2hEavsYpiS16IYvylDhmRukHYnt/XhtT2hQEXdC2kTymEClpKd0+S41oCsjAEdKONN4nprlyHd0fAg0sUmUor7HJEYzA1gqSU5yE'\
  b'HoG5AfV6DFQqnc1MLY28U0yYz+fo5/P8HpgRUDv+WTkhDpk8ZiWRAcECbdAv/9Q3H3399/7c2gg4g7XZ06lmTutwuFwzi5pc2bEFGSqiMnI8R8qKcgMXwP3L'\
  b'NZEgH6rATVx0XIUfVateKwVcRLunHffEcPJ+0xEVda31E2nmFWxTSt2UWkom1yVfWwqkePXWMrUMRCye9BXHAlWhn6OcmnEuMthUJkMBckTREMqOg0FjuKUV'\
  b'JnaivwI0VMvoRG10gzriRdgI7+T62jJvFkhTQkpltnoOfCjbVZDNp6Is1JFWO2S8HjNdk1xdXrg6scg4MNcIh1kjU679ZkXWTk1YLkKqO6otI9axkOdpuXlk'\
  b'MWbVulZNU4qgwJCoKRMAiTH/7IR/OapLhThOMWHCW7cBGAFnz+VlFg6VShFaYpFK8oAmAgBxNXxiiy5YSUEu4ToTNyKywhm46kmxRICbQeSutGXzsTSNveK3'\
  b'NVcAskhGkpWXFZZSzONgtdu6m0z1Cp/BJ8/nDnrsKM5xhbNxUUMTzRST8qHdgm8FaQV+PkGkwSWdMJyZ7dMdrpGLfU4DIrzwU/oZd1wfYxcEdmmWje8hGk53'\
  b'otriQewmfObPMOObTt+k6mXMJKVNQvqqeSHvvu4+phjl8n7n5a++7TuWOr43p0whA0zQ6CWnUe3nxyETysw28zw0IZG47jCEfIFIKp3gfBz5/FEwgQi60OUC'\
  b'BiMDinI3Nqe8/Buz4tHAhgJjyutPWR7n8wo457ZpUk6xZq60zq926thqB5AKmLABDfzJyyV0N54EvpeIXMTUcBu0RyNBJA3wNFdKoabJ0f/OoUYrGURCqe6w'\
  b'cg2kJGt+H8FxOFIiOS4EjotySiXMJQ3kRrzRUFnTTBpvK2lNBMSN0FIKZ9NGSEwWnUlJv0oaQ+RIYZQUI7gmTWZXPjcgZQK1U6WKET0oQYghlPTzzGV6sYF4'\
  b'UmeoJ4svzVkPuF7f1Px99/zdq/tuCp7fHFwkE9i1gCj3G0LlcGoUQ2AOVaHsytuZ6IUSydUnKCuipUQsgTmnjSVqIb1dL6yqsM73rylWFFMgR6yEteURcAbr'\
  b'69/81lv++AlUhT258i/56orjckrkUiOAGrazC9fZlW9rqN40hjr5l/dhaXgNr79xkVj2c9HUBrW50VKETDTm24IiSeBW5t9wGEY8M0pbAEmuthBqXw855tya'\
  b'JEU8kVzl+zwcLSouJVOSSlyZ3NE/CijUpDoYaHEK+Pgoh2s1qvZSaVphaRU78HHMt8eFKjNw+mkhJKIGeIudaUHaVCOZAaNOg/CGgIvXJoy3T1lUU4pLzJUs'\
  b'ZhUBlvI1qbfzYPxHMX43DrFO3RA3ADlHLDFPEU21m50pIJYqVMzA0qOOLNZUOBw1WWwAACAASURBVMWsPI56MV7gdfrln/qWo6//3revjYCja2uO03vbeQQc'\
  b'WJWzLq+3E1xTJkFbqWIXxQSq5VevoWlLzrXq43uJoD1MAMpV3E96yBkUFatTotr4GByZXdO6elVvOAyqnAZKxCOOCyEV+LUNq9bW0KRD7PcXOStLqf4STtRX'\
  b'en3crjUQ8YkC8UD/Y8eLarpFVpY3sG06wev3EzTkKe+ZaxTkNTt2CRAnWYl+5pSmTIitIBRDSc0+/PAedTHh7LXOzZXpxiHzP87pEiNoWshk6m5B4FC+K1GA'\
  b'KOewVglDMELXp7quedTAhRnRpU6C3AtW09j8GXVdhz7OwRyKO4GlWIE4gw4TJrxz8qnI43z+ACfyagUbadSm2bQpVO7FrpoKOBauMoeaHpQQ3sjlOvlx2MOU'\
  b'7Q2kjBUB2gbfMuXETw4owUWthNimY1dhYrvKqzjQfmdG2ZRhsDlzYzfXci7asrG/RpdmQrSjSQolPCjNt7R7NU8s6atrfaiQ4krgru3Aew5zIYm5aJNK1ENo'\
  b'0kLzxalNnFSqU9WaRse/oE7SrJM23QUh1bKxH6v8ZFTDPp2KUR7f74Z3/ofvXDqy8sFJ5ma4RDM5kq5FB6JQiwsa+UAMbBwfVnQzLZcoA/FfUSZrmRvEhT5I'\
  b'EhG6gH4+1884lFK4FM8iKVFPigkT2j0yplSN4I+WSb8gP73Q2HZyJxMFtcamWlAqpVkFquzxQu7KiqZ0bUhi6YHsuTTWdISo9bIt29c6qF1vEhfOgsFcVbcG'\
  b'NkGjmgo4NdIBMOBAqk6mLS+3fsAFgFx3AouUkhI3b9kBkxuVC6pJF6z/qiGnS5G8ESg2QAkqHeEF4E2+oFd6D1TFgtQhe9H66OfPsHE5lq7YdyhwP6LOemmH'\
  b'ysnA1a+MK24lOI+/756/u68GZ3Gy/Vw7B5lqCmXvnTmo0VjmngKHthFUKtTnGeniZppXXVMBF0cCB2JERLWsSFqlqhqeLPSzwX6cS+OahtmesWxuubt6aAQc'\
  b'XxKPHRHLXnJTv2jbNZXrIHclcR3IqqWxCY0m1vLdvb73yU959LqZ5iJJDoAKb+GqNBrpBCuDM1wkkwls1hA8WOTjyGLrkrbnYm5TkVJuptpO0AgiHQDW3i4p'\
  b'Xdz+HZEju0mbD5tmhTKhsnb/EEnjJVNf20U63JLUXGYmsXJIPDBSl6biWCLOorsx+U02Pcu6FR2/48zGkgrkUhHNuekVZfihDN1EGzFh6Ohj106n1k96jkac'\
  b'H46lxhkoc6ncUhoUq1SronFp8yCgziHPH47jakqJHjEmBA6l7J1PjtpgHDggIZY2HWZSO9naBCwKQgcn5yZPRcDhz8eLvuHNb73b5zLEDOJcVizBhfoDew6w'\
  b'CAAD6dWFs2WlkbBaaxHHsJrQjVwaIMDAC0XaAlXxJpGmM5zMKItE06IauVjUEjiDYMgV0Jpmcd6UIWSTSSZCx5SrHQZSrH/T99YxYRJyL0/H+p/93DG6EBA4'\
  b'8wsTZkw6uy/p/fRYuPYDdYExYUZnr0uMwIxJYHRdQBfyazPZsXEFT2Z0IVdo7HnL+zCNCtOAz6I9KmpPqtUxxJUzyyAjxV8oqdmZYDjYr6JKcUl0FqJpfwuK'\
  b'R//b//19W/vd8O6f+Y6jxw88sWhd4mxEsepwjBMuDZ3akoISqXuQk0o4N6kxCtFvXFC9OFb1sI/2man6PhePonqM0PS90ZgR4Vf/zbccHSMcADuRjlWbyyo4'\
  b's+FoRpoxKh9T5OXwjYutfSaoRdFafJJCCFtDY6FeRSp/7CMbJzysVSsugj5S/QUDJXrpjFiEblYX3ZhtA4Pye3Qq3Cb6odp9XUrursYShmVebgvKRsqy74sa'\
  b'Niq6z4scl+N5ZhpUxmpKSXuIe7tHwPVHdfuN6HHHLBjqrK/czZ//E/dfKreLsyERQRvlQOrMdbfxASCEa0c3Bxauvqi8Xy17l5YWiCqKWTlAdpm4S/mAWrHy'\
  b'xHhx6HNRDVGZLGFRUyrcVP49qk1pjqKyajClCFZvoFLlitHpc/L+6Hj2lCOOPy+AM0+8bJ97MMmtt8t0Q9GIUHtWdEdYedzJVAajSRQcxHnrkOuHctxFm9AN'\
  b'RwkPS/Ma8eiVjUupt0Y2WdCWoxsTt/nmRYt0DJhY2dNgqRa1nePBdagS2lK1qWSKg9/QbsIPlWs6z6XpEkNT8Rqyr60hlmWrTNjTV5a8wG3AnxBM9UvVZ1g8'\
  b'IJiPsyBq+hRj/jnq2BpzFMmgkxxQ2d8djyMtMOmRfOx3rhndfOfSiYMfP8QcGmI3p/OaOlEV6PnJFIFqddCPKTbtWNB0yU+9QQP0OgvNVdOYGEnHCefyeCx/'\
  b'jzrDyvZEbm+o3eNmSzvlnSNjhANgK4VlL4yyuT+tL4vAxFeF8GUnNGuGOLdfcGv6LYXj8Fda31FBbkLE0KqdXGOpifWYvS4oA41FYxlsaqRTQMaBk53QlnJV'\
  b'qX+taBlgMHxFijAMcMgDTenZqt3dteG0WpaaWlj21o73VnskFY7M2zDa2BXzIxZXSUqFnJc2j3Dpj9EZBhIpubRIBL1FNzHmMT8KOHE4SNBaARRUog7/E8fB'\
  b'6br63/+f77tmdHNo6fIrpl2kmo7nVJECl4kUwQy49HustrGVO6zRcOv9TMxlnFDGXRp8h7kj0zQ5ULmHMHKDcTH+SuU+pXHTzNiomvAzM5a67QMj4ABY77uJ'\
  b'Mf1mTyeSdQ3lisj1JPeRDlElSdlvIPezOMMnEU8Ru8mdrqZKPrUjajZnJajRdDmz42csMvGlb3Y2DIVMLlohnYVdJP819SoxRjG6QjXU9qAg+yQt3iwObnYx'\
  b'tRojkWELvo7BdQ2uPrbJc5TKMIEm+EmS9jVA3w/JSOng8vqwyAbFwygqX5M0pcrpVbakyKNvpEw2Lc6OYoBVI53koyfInAjvu9b5+F9+9ttvvmn140fIN9S4'\
  b'AXym8BVXHa0pPZf0R1LKfKT1g5WJoZYiabyso0EKn082EieV9MnaGYqBrKZkRRphn2s0gFJVslWsRDDtZpMRcADsJiV8S9mQylWjKMtEivahiukG7nDkdQ1S'\
  b'iksW6pfZ0v461ziVO1FYfdCeClClKqxlQYoI0SKcUpnSKIcG1algkQ57bocKD2InY8kcIaXy1Hh0iNTxtQNRm8eh6ttLKm/FXi7HcR/78SsNgnEVuNHA8Att'\
  b'/Nh+vLS3WuQjg2ztqpNB1bsojxaGcjZSQCcJCtgknd4gjv+JZSCgjucxEIty7+/++N/Zutb5eGz53BfukRygtseICLoQBvIJKfbVpguyc7NJI10ULX6eozWR'\
  b'OrN6oRrdkPExYKf9ykJIAmkPlUoJOJQJDlZ4SSI4OLkUnvGA8w1vuefuD1/lcqIHCkW5m5WdoViJeiK1ofkJg04oZ9LlOnMbJsI2lBuOVjblsNWhZNjaIKjR'\
  b'lAcL6x0K1qrgop4i6dcIKHjlMTzYwDWlEprYymaYOyCtKYIUQ/C90c5+U0uv8bvsdSREGxSVyE6ij7DkmhFX85kObyOzf63gloo3sY7jKYCShwhauuQjG4tg'\
  b'kqZdKdVpp8Pohoje/7s//ncev9b5+N9//hu+aHXpkYUs9qLiSkDlZ9eQqsAbVFHsuSgvp/CeQ+IU4xSogKABkk0a2a8LJVBuaTCDLSu1m3Yn3zlWolgogy6q'\
  b'//W7f/pbbvna7377Y89YwCEibV+oV0Fj9VnqbCmmahORu28tjCVnaN7644pIOWlafTs1G0RqV0u5Qhn/AZ3sSBppCZwPj3v9Bly4BRuvQK7qYrg2h4FnsaU1'\
  b'qKmFm+eqRyplBLEOCN6zofc1By+bXfbnaq4FVi4aigP+pnEO3O9xA3WwP8hasrZJBW2FCqjgYXxMBRn/byqpVE27KvGsVar3/9a//NuPXOtc/M23//VTJw5+'\
  b'4pZCpIugCxZVc23hgJ8SWqucbR+Zm5DhUMhK7GWonl5hRFLlHZPzH1AXP7jRxMPo3Kq4KdZqmQ1kNOCxEdKEtPCMjnC2JZyixgdFBWPK37CZhhCBtSGzBLk0'\
  b'kOubMx9VI6w21bDvaZBWle5za8D0Q+FqI0+daqvK2VJFMhGYFJ1K5XU0zWJq+Rzja7y/j7heJpce1SF4bQpSdSdUZ4KXTyPt2esttrgIznisfQIg37HdfI4u'\
  b'anmyXQT7jWIReJFeKtGa6NcupfqEwulAMAAZ9ciJqZTQG04HaS6CD10PbN7zH7996cTKoy8NmBFz58AE1b/Im70NxtjYOVCoHhlOD6UCqI1iXSpcp1IxlVJa'\
  b't/Qq6eTUXH1KrYexNnSSzagvF+XqMGhTJxbC7BieQgPyPueAw6CcNskAAIpSsjbFFbXMQGtTxpCQ697GkF8Qz/vttV7AsO/PVamKz4y4jmQq7nzs3PvM1cKm'\
  b'B9QUEKVfKF8ppZSEyamcZSBANPWpJNRRNhav2VA9pLYC4yMcp170r9NwKiL7gswwXql3lX2K5PvwPE3Kiz18kHggEg+w0qqENcox8V4dTjhIowxskkU1QEpy'\
  b'VQT3/ta//NtXrnceHlm+8MrFbn2qeR64o+JPTE2a7ttbqLX/cNUw6+tryHuRosA2AWCVEVD5LsssNtSODa/bySXy1PjllO/Y5rkbmy/VhSCDV5o8syOcGA5U'\
  b'8lFVxm50B1Ed5dFMKvDpkudRreTtNBAYTFtt6YQq7GMXGpeMgVtuIpRUbtARPUiPAnxKRa2s359sdozJRd+lnJwacBFpFbiFwUn7QKXsV77yfEGbctH+NE4D'\
  b'VDREkU81z8nPZkI7/6mNLlGis+I3rbcl15jpgUb2SasKqZysupU+HpgeeO+/+J/n1zvMP/iFr/4rq9PHDxECghln2Qhoqk2y1ZaDGh+lYrkqaEYnV591alpo'\
  b'rA+urRBaH5kBC4GQGqO3Jmd2c8+kuTnLR0QN24drafLUKo1/zgEnEQdTDIuI0yz46ZPU2n864Z75z5Tmb6pNf82Gpjasb0BFQ2DxWjm413K5eU7zfGkejcKZ'\
  b'ZG/DaBmxwtWJL0GyiC8Jkg8TSlUll0tlQKLmc9DNLXIo4VMu2v80LRHb/qT4EEL2i3wsMkvXxptmdI/vEfKeMC6G1LlMqXkv4oAWDdCIiEulpK1gJUGf0sWY'\
  b'0gf/649eP6oBgN97x6v/ypGlc4dMvd6MlEY1SfcXi2ZGmnMK8DPLik1FiWKk9qI5/xsDrOQ4Rsp+qPkv5EbFiOdtqAzNszYLCgKWTCxnq1NucuoUEwLH8IwG'\
  b'nBretx2uPtWBffBCg/4b3QLmF9M0HVaisykJ+yv5sKrg0gGff/urVuO05/1eUPM5IvM2rqZQcKG08TQ2jM+nQ6IscMo5VBGtSZJGhWuAmUpjYyWWy+A4GbDB'\
  b'Xto6DGWuUUnatyS+z/SHfS4le/kbAyrQUIRXOKfyXorBfX28yLWjmySCGNO5mOSB3/iRv3XhyZx3v/tzr/7Kw4uPr5IOs/MfCWsvH9hNrjDdjZ9TZmBAzr7V'\
  b'fb5VN0P7g7ejFsTJQgZcc2PZkiSW27xhOso+IqR+qMfKf5+GyM9owInUMSmhKrL3OltnXTtbUfelVavQVgwntJeAvbY70977yABoytVLvXrJzcDa0/JgYq5B'\
  b'+DtopBho7aQBlzLaSklT01IkaVPB5D8rq2Y077OS4dXIatgyjdogu7co5a7K1XbVt0Ps8Z/ZD5TEtTJ4MHL45/mrZmihuy0NRH4iWEspPd7H9PB7/8Xfmj+Z'\
  b'c+6db/v2pdWFc19yePGTCjZURg3XEcMBFBbRTVbBk4M5wk6Xgf6qPzFr5NN8nEPLV3fxk8pFlv9Jq8FpdTv7a6H8V8ll5jwaAy8z+nJejlidXnlmRzgbcRoI'\
  b'0hhihdKrUklKa5KjMgSv2jSCay5MJNU6wvuPNCnFYENdv7QCU2eR6/QlsSZSb7NQ6z+BeN/qUHLVHgyrTqmS3VYOrlf4VEbbRN99mE/IPoqsF2IcjZec82MZ'\
  b'krcy2Cjtee1bIDwYtFUuatKla0U8JEn5kNTO8ZI2xpHU+tWI1rMto4gxXRRgDuDKr/1f3/PEn/d8e/fbvuXmo0sPvmx5cqUjjVaK1CFMEBaPY/Hg8zA98Bx0'\
  b'iyfBk6MgzlowmV/C7OI7IVv3Ve1Xk5c7P2sDe+dwUCdgDMbUkLhJZ26WhvfuqTOKysOCehfXJlFSV0AfqZHKCoYs2jO1SkVUiDlx24T8tAY3mpYGo2VFGcbW'\
  b'LFwqrrvqx1CN05Bv3PKsjSp0P60KV9vNtqGJqoCLqDQwQkebMOpM6+Zo3dTFJC0vY6SppNQnpDURuigiV2bzeOkX//H/OMe4ntR67898/RceX/rYHZMwJ/9d'\
  b'cXcAS4dfhOUjX4Tpyh3g7iAoTJsZZyAChWVMDr4Eu1v3lykc5MbAeC/sch5JldA0RmbG7aSBLUo5H1IZmOirkOaLlCRVyYY4EzJ3arOz4hVxnNEzmsMp3Ab5'\
  b'X9vKSgEBa3uQMmvc+Mmm3wWtBkdkWN71A+zFtTygGRrf9EbsW9AlDFp+LTap+haXSjESkkspatnXKW51rIylDATamad0Jkb55M//w2+6PMLGn3/92v/3rUeP'\
  b'LJx55YHpR3PZGzp9oVvE8uEX4cDJV2G6fCt4chB2ichfUO++dMq/SwJxB0jvCGLeW0kVNOVz3wwM302jDn00CDFJK025TO9mWxmupLbmV0nkWslN7nmpWK/I'\
  b'MxxwxMODvxJ4h72aD4uKXApRR66De1AGbyJ22gswxeaRXIqi96nC3rb86bjqlipRf10hQRIqw9lUWVFL2EjuZKzKWjj+RnmK8wJ58O3/4JvOj5Dx6a1f/fd/'\
  b'Y2lpsvaywwsfPNbRjEC5GhrCBNOV23DwxJdh6ehLECaHQBRAMtdopgcwAcH6k3LnNeIW4s4jQOrducB6rrTT2sVNNm1iaiEHLlLL73vI+bYBNE9p8FyXzd7K'\
  b'mpzin6OD/szNoJVB5P+99999y91f9Z1vf/AZCTi+14k0mmnFZVRUxMJoU67B0LyB8X3rcTNoSqyhkTRhsE+VUeYtSek4r37LVqrOmhsR18TdeFzV/hhqCDxU'\
  b'nYlGPEmA1KfH5il99D//o2/eHCHj0weaxW7tZUemHzgWeJeyHW1A6JawsHway0dfgpXDL0C3eBOIJwoAveNlCEAPQVQ+JoGkR+ovIO08WE3QfK+ZkY6u4kfD'\
  b'Zqg9HJv+Lc952dt4UzRKqdiSlnOpRCtUVMeZomDt2K+2HyUq9z7ez9QIJ5gVow5vY7ugDEAHZFMb0Ej6fUpG8JoPgk+p99C31BKhTUHJSjoyII+NFE4JULNs'\
  b'4tyAZ/OqkyqPE9sIXW7pVXHgmqobnYisi+BDP/e/vXGMaD7N9a63vfH5y5OLt61O3rdoFw4OAZOlk1g58mIsrT4H0+XTCJMD4LCUgURnV5HEpnDRdsBHiETE'\
  b'7Ycg88tlGkbt0tcOKxmILmnfMw/tMEI0CmV/1iexZmHUXqvaOVfkJOn/b+9dY23LrvLAb4y59j7n3HOf9XCVXX4IPwA3brBxGgjBYJsEhEmjBBRCCBgwrVbA'\
  b'bZRO060IUCeNMHQrUQt+RELpVgKIh7EdyjExIDdRBBhiTLAxGAowsU09XOV63Kr7OK+91xyjf8wxxxxz7X2vbShcdW+tWSrd13nss/fa3xrjG9/4vkAU+1g8'\
  b'q/97T1M8vajjp0CHo/Gm0FUjXvywTpaRo7N/G0kjksnROPsacv4GABOLCqWuvXLDa/KX2hslcf2PQGyALyipCWoSWp1Md+rIuxHF+mdv//6v/6MZMj5dbubv'\
  b'P2fBh3ftDlcu7A0P7wz0B1Q5lbQ4hd3TL8L+Lf8t9s6+EMPyPJiXLclDjt1Cltp2biiTJ16qcoR88KctGqemY4Qs8+ihROH66WYUAWVaca/9DVBbzLWaj3NJ'\
  b'Y0Ax1ZLo56TBpCvQjlxsSFoBX745p6cXcfwUcTjblwSjSVZ0JlFq8Su6RUejHbo0EGt2OGF5k3ouqU24NOQyoS3kKUFTABpRKJfJE5Mgm7Of5JJt3YXZV+7R'\
  b'BBRZ9IiA3/mLksHveusb9oa0ejbT+szA632/xuu+a/0zh3dQJ/igziKzsaPdvpBu3ZOaTsBFNnRFmx9cN8Djv0yFiL1dRlyKYMqLJR+cAhSnFw8vVN/fYmVU'\
  b'Cwl87nNw6sLnYblzC4ad80iLM6C0ACEBNXfc5wEZoAFAtrz3et/h7nUDADl+ALr6hN/M3HepeteIOpfTfLHbioRXtdt/zI0/xJtU4pK4WVo7igs4HUuArloK'\
  b'OfPxaz/N5uKfccA5zSf5qiyTBmvQBjx9hEtsSRQTsyJo59YXe6TpVw4Igm6ctA0NgyhOiSBVj2OtnxJs8mTpi8LlY1CGC2KWnn53M1HfKPIwE3737v/9733K'\
  b'Y+1fevu337IzHLx4Z3H1/P7y4m5KH2j9PGLAX30ueKs9jjv+Uy8si1YGPv0g7gDDcQttuTOb5WX9e6l/tsdTY13q6LiafdfJI2KluVWTqW7VqYaJdfKTluex'\
  b'f+6zsXv2RdjZfw4WO7eB0wLVnoqQ7Ouc2MvMIYuLQHI44W6MNNZmGq/5CPngTwBdWwpsVCJN3vidhW1orUJMUVd5d1721KokB1y0HNKQA1/bpum9W4OivN8+'\
  b'UWz77TMOcIYyvEux+Kj7NxqSB9Tys2NiAnRLEmZcZ4gVz9ayyvrc2ovT5l1c0XK2652tVDXNq0eM0GbftapOqWbyXu0WcnXe0Qd+8Z/9vd/9VJ+j/+/ub/qC'\
  b'0zuPPWd/94OLDoC18VRe5htvRKQADRiWFzAsbwPxotl3OOBKZ8daY042BYC0CcIBgESlrVqEdMziNiGoXiyxWtEsE4Cxu3fwzCAUfozDK8q8g+Wp20E0YPf0'\
  b'88DpFIadM0hpF00DtbYJEwFYB/Ww+kJkU6HnVthZm9wuKAY0Q1ePQI4+2g0Eoii1ttzaLuBAGAfXggAgm/iq3UCjACqbBksb+Ol0Qqrd5Jyisry/kP2sZPHg'\
  b'MxZwOpPpa4hV1Y2/jagLiQt1HKlBMrulluml5KF7CGYP190jUtsQz1K9kdv+Elu/LOYImOvN0u+yzUhcVO//pR/8++//VJ6bX737m77g/Kn7n7+z+JOm4qjj'\
  b'VxMfSR2fUim9Fzt3Yvf0C7Gz/1ngtI80nEYa9u05SoUcjU+K/7wZREMwKe6THvrnJHfrFe1pykHzFFul3N0M2lOuWxbaBYoRsdsN4TXOz/T7FAaePkeYrE9g'\
  b'tOawjRHJJlGd3MHarsrlFGL/CPnwv0LzkaeK9txhaCFL/9raVd8FVGzpmtqysG6vlBqfU82B0G2JV8/iJjDs7UvaoKOvbr7mO3726Jk7pYJkJSzqsmZv6GRt'\
  b'gbaLnza8FyeEMHqSbGsdqVtco6aqLe1jcVXFFjK1I5WFmv0CUdxxgitFqdlI3P8rb/6mTwo2v/Lvvu2uW/bvffnezp8mN41H2SPypE9PgFhgWJ7B3ukXY+/M'\
  b'S7HYfTaGxXlw2rXHOJa9IM3tR+yifikYcVVOY21v2C1GxAGgyq+DvaHVAbF7FWScPM/j5DWKHdu4BQkjqIwObBH42mHTyAwGOmMYM7QNd999U7H2KVY01nYZ'\
  b'UOn4BPLRxzo3Rr8KaSuB0m5RugWU45E4rWr7ThqMuOq/sNmKQqdg1FMOza56W8AjnnbLDZ9xwNnl8SoEu7rFw9WfuolMnH3rGhM7TdpapWi8OibbvN0uzDbz'\
  b'qKlthcA4mrYcqlqmUm4TSmjb62176v53fwpg85/e+Q1fetvpP7jNx62hwmhRJAxOu1gsb8HemRfj9PmXY7F7B5h3QgkdqgQ9ATCAyN6gOgI8tDs/AtBibGSq'\
  b'hsnHxotTtSpjByRTnUlptdZbeKBKyquBkgK0BOQEbTVRYSqnDoxoM4w5fI49/sImRdPZ8HwQCKMBjPqNoVXIo73QGfnoXiAflNeTWiCjbtlNZaJA21QHxk1r'\
  b't/7yDiYqISWj8pBMsPytuoBL/vvCsSVkc4NsO3O0dUGw04A9Y6dUhO5O0GGF9pYLNCHaozVXeTFkUkJuJjJsfv+mOt4gKrVXZDTbSBP91XSFDEj15xG70Wrb'\
  b'Dc+ql//T//nN1wWbd73tDXu3nL7/1ad3Prpo3jube1xMCwzL8zh19nNx+sIXYrl3l+d91zs6IU9+RHLQ8erAxkX1c2M1V960odrQfO2Xz+X/VZRWY5DZdoBy'\
  b'ARb7Gop1N75t7y4G5GqYDMafqd4kcqiysjFl9XtWBW8O/tZxtF0V65W3CfyO9m/+er3I6hHkg4+ESV6sCCeqGu2tbvtoBkwM0XTDJD3Whc3xL9Q4Jg6EhOqw'\
  b'zuJtNK6TJM/pDZMAPHFyNj+jAWeJ/LiS3obgYBfvEqS0YbYQx7pM/bq/A8fURIuvXf7Wi813doMcvAuVC7sojSS1DWGpZKmAMkdT7fWY8/s+GdjcdvZjr91f'\
  b'PpFi0eXclAkRU1pguftsnLvti3Hq7OcVlSyN4UK3ioUmiO5V3bFXFQXIx56qqbyEjtGZbGM5tbU/gWNRWGUQVkcAqI4gGqAYG4fTtWligFQrmyN0Eff+cueJ'\
  b'Wjy2Puv2OZo3q5raAvqNn11ZXCoouI6AVAFN0PEy1lf/DCpHARxpk0ivN6GgXFfZPqPAZO2m3Q/rGF2bT1O4Posfl1VYlC1coHKYQfTXAV14H4UuYJRBntGA'\
  b'w9AT0lDs6XQiPdVl1EurObIwWnW8+SprEPbVG+JE/BMFWF3p1SweSQBlDXlC1FYxwvZ3k/75Fvjv/ca/eP3htfma1991x/k/euWC1yFut13btZQHJ+ztPx/n'\
  b'73gNdvaeayAw2sVvrQ8N/nfl88e+IfJqcZy0RtLvZYSvXTChVRYEDsCxDq9GbGyt8ZWVaZVOACSvhtpgKm+2utYKddWV9hwQ1TarVji0hoUgI5QA4WeO1ZhH'\
  b'ZFgbddx+BhPPqRxhPLwXunrElMjUVT7UEemNP/E2qgpLPYoo3OyIgn1FM1OjTuXe3xxrpa2Ws1VJbdlS7Z/tSwAAIABJREFU0TAVIllztvtFv25xMi5Xz2jA'\
  b'GUgeVNDLKPAy09jZRqpRNzEihFUH3aKf2SA6t3CR3WBSw5s9Bsxp7x9lL6D4aFrtLsXIIeGBCA/9+r/41gev30Z97OULXnVjz+Z/XN4cTAk7u3fiwh1fgeXe'\
  b'rSAcYyqNL59yZH/P3QXujaGigATVcfW2DjcDSFAdN24N5qcQWU97g+dQTdRqc21tlXr1Ff2Iir/H2hMpQupVGATUv1tvjBoaMCUDinVXsVCtWnQs/FV9rPX5'\
  b'cfK5cEdleidQGTEeP4B89HFrz3iT29O+XWErnXyautmdb0g24ji9K7aj1VF9hSXKP8o1KRCXjxAx8jj2r0FMTnXajLDOi6NnNOD87Ju/6+iV3/vWTgwV3ysa'\
  b'RuaUahs1SS7YliIZ9zKnQqsQddvTjr3itXOeiApO6nmm5mjRbCmUdL1eyweu97PffuZjrz1lbVTkqhBigRWEtNjH2dtfiZ29Wxr/0K3rUAAL6VpJQgSebBWK'\
  b'tLZLecuobmVvQJ08mVz+ras6k31m9vxyklJJKI0gba1UzcsrYKZtUtTdBWiT1YiAVtsvin+fbGrGIBp95tx+9mN/rEWHc9wBaa34VEfk44eQjx+Eag6eTHVi'\
  b'2vM92tls97wMTVM0Ip3UJTlsv09SXHWgYMZldrrJ4mE6I69qyyqyneeEYp0XB89owAGAC+l4/cS4XDQV6cSqQjUsVGoLft9g2yKZGSYaFAjo4M1YdRk68Zrw'\
  b'RKEJmegDSJ3Go2hnGWzV0J+878e+45oq4l9759/58lM796ZGOrYLuq4aqCl9T597EfbPfJbdpdcbEyMiBnRlFykHHst2y9SYbLuLk/m69FzJtHIBOh9NYQO7'\
  b'MWhr2B9PnUYVQCstzibvomE6tQ6MXOVUFlAc+2OhlqQTKh/ykbI7EtroH7oyDidUZZEnwjq8o81mX1e2qS2Qk8eQjx4CZOVLkxtWJO7VWuC1btYh5FFtmUd1'\
  b'W9rblcbRwkIccKKfU/32LaSj6Y18n0ppgysgNJ5z1OHKMx5wBhpXiuUiPjFN26LbxXhxY1wmo9vAyNE1JmPue+zkJvkuDAcuYlOyo/2o3YBCrGqQAjaHv/Wj'\
  b'335Nv5FfvfsfvvSWM/ec97d08GX2RVUCFsMSi+VZnDn/OSCs2mSG+vU7CpOn0gEI6k9BVYyWjW8hCVWNdCPtMvUYt1Q8bG/mcfJGrr+36kLH0ArntnKhNtbW'\
  b'0XTY4p9HWITRYzYRqDTiXsYAsFVx236GsnjJgITKywnvwiFBpG+NQPac1ZYlQ8arGI8/AZETn0R1xC734+gYS3TNlgs9ibtZhIedKN3kmYmwITXonXPawpzW'\
  b'Jc4svvagcVvZPu0rv+3tH3nGA84ScgRgXyct0bV75+4G7jc/2vJ5RDRx5IsetIImeUdHoHhlJM0QIG521TFwkd9n38ws2Vr4k+vxNhf2P/wiCiRfAR0zcgIh'\
  b'pcJRpGGBvf3nYlgsjQAeXUeCjlwOfjsSfXINIGQMU5woczV+RFtaabP770Z7VtlMgMq/3mgj91WI+613dQ0K5OStmjrhfBXAIkzMoiPeYK/HqgEiqJHVVU8j'\
  b'sQev3Ewdi6fAM+Wev7HAOFlfwXr1KFRGJ4QZCOPkGGWsgRqL5lmTEELVPu0+6L2ivIM2NGX2B9FeumGiv9oi+R6VtsdTdtraknC/3gBcepqNxJ8ywNmh9UMA'\
  b'biMNauNpqxRuOe5prL4J3a5HULsLhX/rQKd6EmsbGSv1mUneo0cLg+D0pdBJIqPbha5/+//+jvuu9bOe2rn0ip3FAceUiVSdC838nZnAPGDgPeyeepa9aY97'\
  b'T1xdWxshXUnvbxcZ0faE1DalB5+goRv1ridcjniFsB1sAt/ilebK5QMkI1RjC8a+49TuJ+uNygXh3wthW9tobmCLhLoy0t7dpi6W4ltDYICyc1Tt8qmVVQWK'\
  b'jPH4Esb146gZm57wquiCGF2sWBczRevCnE+Npgs13cJx50TQNejbXOMmbHM0CCzXYhmVtwlaMekia6tKEEEO6Zsg4HjcXc+AA2BB8iCAlyk1nNBAdvYZVSF8'\
  b'jFokcFu8DJEuNWVh2lzRZOs8lJypXhKdo2BQToUlv85Mq/X71wSbd731DXsX9v/otnrRUqhsOFXbg5KHlChhd+8clstFmUrVi1/GQAAPNlpG2+ojtukMG7cz'\
  b'hhvhIdoqglUzlMK1XSZUPVEQKx6rRCp/U7kax6kE0Gg/2UkgpavYTzp5g38/teqt8nOq5WerLaT9e0cQO3CMTYujaiPzlVWmYWGARie4gTUkj8irA+TxEBRM'\
  b'0uJkmhDtH+31F3M96hwgN+M5qItupu4lat/gGlVR8MshmmxZxRgZW+5sZPEYFjinvt7AwWr/8RlwAPzsD7/x6K//Lz+DFTh43FC3sV3zvTutSrUKoJbuECco'\
  b'XfVT24iOvylkqFKM0KUNtTFFTRA1X1qlOA3yDKV7r/Vz7u9efsUira2qYYCpJXKCkFLRuCROGBZLDMt9K9KOjYsI/ANRAxuvQgSkC6ieTMjJSA4f23My2Bvm'\
  b'xDiQUBH5cyXdJKoDEbKqSE+C1mVtsqdWeaimUI0M7ZVTMbFdBmTdvdkLwKTJ+DvoWyDlsXTka7XROLHWbkATBmb7GUZoXkPyCuN4As1jsJJoKuHO6iPcoAhT'\
  b'QnZzIU9piiM0MUjXzWFHbYUpbk/B9+e8PaUthHPgMgtxrCFORrt6a5UXj82AY+dMOjm+KHu7MRK166G5XZDsAWSmwA2uaxRkgZtRL+3PvnleV3XCTqZUabkW'\
  b'I3Ql6fURbjtD/V9CD9/3Y2+4fM1p3OkHbi2RrOwXDw8p+KsQhiGBOWFISywWOyg+LoO9MUfTrKV2zXv1vW7OW14NrEHYaa1MR/qO/V1ZQ4vjk6mVVSAVpFJr'\
  b'q/Jo7yw2bwQjpEUA5BJkrCMIa3+ugbH8HNWUHBwGYgpQBukAxUl53DgEYdmmaBrH5BouWQVwaN8j2TUwNqCr6Rh5RM4jsmTURMF+CRO9ZilINDhmdIWJ6ZTp'\
  b'DRKuXrw3dfTXWAFp2EDXbkJaRbHafXwBKPH4oGwbDoFTquaFwaD9K7/9331kBhw7uzReJdXdKurr4lM5Lqu1HGeuOgkm39bmiSctBfutKOH196ple5OWVkTN'\
  b'I0dA3m1Ipi7ErSaEKmmviFY8dK2f7z++45s+f+APU0rJS+thSB4xUjOsF4uhTMpSQmI2bc2JB+8VQnFlFYahpVciVmVA7I2boTiYSFxyIGlza2v8jVwrhVpl'\
  b'2Oa4EkCHgC7aaN63Wo00rmkGqpM3YX0i1z0RqlEVTf7vBTBOrEo52qwOfKpmFVrYtAbGlphgb9QsGTmXEb0781FT4XiyAVFH3GrQ06gbj8mGMLV5cm8MQje1'\
  b'Nh5zTJ3FBoXJZxf7rNv8WnSyNO42klseV/k+jx3d8rTML3vKAGeH80MK3NY50NVpi1arxeY9y6SedpkCp0MgI4+bMpmJN1Yl/GaZAK53BlPGqjR7gGqUTmpz'\
  b'KhXvNnwMXoVtzNfMtN7buXxHi/Ygu2vBbUjTMNhjZ5AWDofp2FqPehuuI+VkSl7xkDRUtbOWiBPVw8lFPoYrdFVsGQiALIIwjm0iRJP2azSQmkyoChobYRzf'\
  b'XYu2Ja5Tpbg98TIRbWpMSK0OguteADolVYP9iIQKqBphVWGcxvWEoO/asEvvvkU/ZdowwdVuJ6ZPZuhWChBaTr0GIPUmaIjcEbXo51pm1xtlfTQcmuGpqVcl'\
  b'rC+dnHt0BpwN4lhfVptgt6KI6z0BWJxsNbtFRl0DgKcT1tEl+VQqGjYGX19rn8ReTL+pSNWySLDAKfdfsbKaUSdrjJzlmj3y6d0ndqnuIkExJHaBX0oDmBmJ'\
  b'EyCKNCSkgdv2swvpRms7klUvCKRr7fzT5gSn+7gR0MEmUHVZMr4ZFtCqX+m4h5VdvMFxRYIMoQOW1YZtSANw6dXe0cy+++jK3WmYENfvGxYWBag2FNUvxhca'\
  b'fQM+WE9Qq8zUVOvRnC1qzwtOcNMGyUTuGe0kOrnEZqEXW1Wd2OoTaT8kkZoZH5JjSQPoqqc46GQDcLvsUHE87j7wdAScp9Qu49Xf+xNfe1UXKfbRZLniRFre'\
  b'kNY2JSpka2lF4KbWbDEtdSs6Zj331jflvqfuxCch8RIQ2N1RC3FX/mwfp+EuWrkelcP3/tgbfnXbz/Xuu1//nLsu/OFfS6ksGKaUbAReFKJDGsBsbnaJwZSw'\
  b'XC6xt7drb5ghvDqjA075dZzIB6JiVTvg0FCNbZgfarNNqEJGnlQFUGkjRFcqa9hl02nlH+J+FNPcnmZnMZH8a7D3MAsGNXRx61kJjsJhJSRyI0wTXxjlsPJL'\
  b'nXdwBzcbYYzbeqM+r5um63tEvfTUgC5Wat1ysmqwahXnrCp41kVNsgo8mxzC01ttnaGkb5oIwEbkB+Mp/eyvf98vzhXOtApI66tX14tzbPwIUQwcKy9topL0'\
  b'RMxFv8KEgRkgYLALjIlCLnTzfKl7KBLKT9960DYazQIL5i0vJCWAhNyJP9f4FzJ5e6mgrrkUtzMcvKAADIET+xslcTKrSDYgYv+9B5lRIXSleu3Y1MZTPAHP'\
  b'xYJoL4NHjRDWpnMM9httf6vP6FIDVbdGl2DxmrXtj5lFZ6wLakQxtrVAmGhNYuVCsZVt1YnG9Yjwc8WNbaJWP/hY3Xg9eFUb1h/d0bFPiGrF2tQrWzfUw6pT'\
  b'OzD7HKIGIyZU9AFD4ImUHK79hkdRawHeumSrpg9L4GZOjxCZtCUW4vHjC5fxND1PKeAsKT8B1XNqVQlR2LbhVr0wExKXf08GMKXKgaUMKFI3QmeTw5dXORkp'\
  b'7HdqBkRKIkNpuQWitTEh5JoMwQIVKplTXLOoLPhO6dK1fq7EMlQgIQBslRqgVt1Ye8jJlMp2UYpAiV093OxL44hUsdXFUps2WnW6TGgG5SJ10bQ3i48WHgZs'\
  b'UPLJSAGvULybbUL1eFatCV0N1+Jdn7sQQuPiroVP1HMs1Gk3qXOR1bCG0BGqqIVZr6vpK4yGZqxsVVMcULevR6Hy8vLGg8tC/+/WpinQOdwl9nhFQ2EdhXJ5'\
  b'7SVs7lPLOCOoxcbYTSULNm0u2xNyuD718Aw42wHnPiJ6QXRUr2bRbK+jt032d0WlW9ouRhmZl8lPAaM2nGrTAaKWqqBaKpnEWoyNtKRntigRBkRKtVNTBdjS'\
  b'XlyPQyCVa04BdofDfXJVSJ2mcWmr7OdiZvsZ2cPPxpyR4hszCNRc5+eUKG0xY2oCa9Ho/dyaCinu717J1E/QrKZmra1MbS+l05JQWPEgLp4tTOSzJu8oDATY'\
  b'988QMsdCmKGDEzdDrGmKJU2SWSuYhcql+QptsZfoGZTuy2tIptRJdrSGBVvEaF+wPW2mVCa2n54tEYSMUyQzKI3TcSO1qfyqefSFvKLhNBmBxgqGgnYnxtUA'\
  b'E6EaVnmJ13zb3ffMgLPlvOVH3njxVd/7k3oiQ7neJs787O1SAQxvqUAGPJFA7s2saFv0qpS7NYN8yiFhA5esvVqwGT94Lgy1tEQoche/spUZU7bVhcSpEMTJ'\
  b'FMWcSvkf1hY83lUEYtVAmU/lxrVgGzGp6O02rfkKudXtgpXgzxJWDLROfBTjWN5+0lmrNptNDlID8kqCOxvOOAvy7W6aOPJRc2UkEJSbTjaigU5WW3wg4EuO'\
  b'5J7D/QrBZt5VdJqIbRepTvQ26NdG/PO4tVX+M5NV01xWOyjZ5yWfMrZWX8P4vrr3FUGnsu19afPYkaqoJkLO2cGGtmybx+jghw/vuISn8Rme6gdwLh1fflhO'\
  b'n6t3rQIgatOnADoGLqm2W7UKsrE4BXAiABvRSqrQRE66EYriGFLAgxWWD15aqmT5QJxK8HkWm1ZJ+d4i13bDH5IkosI1VbK4cjewqqY+VlFBMv/cLNIuztCi'\
  b'qE70GB23kD3PKVhCT9Iustc4UtMo7A3Q8S9oOVcuEaj/ceM9yONz1U3km54mPLcAGMF6o1YgNNWPw0Ep+sywoc50EqMmNYg6l8rXucAz6n4tx75bINeYaEH9'\
  b'AJta4L0T4R4yWA29qAEN2H6u8j8pA8w+jrchQ+g1q1Uo26SVoFL3rIayvkEJOVv6poHbljFYP3Yn4PLqzIdnwLnO2eXxYUDPdfnh9c5gvI1Pp+yiaS0X+Zi8'\
  b'ugG2WJW2y6msro4Vm5SIKkSp8DdSVxw8pgGSGClXb5k2Pq/rFNcb7505dTgwD/5mZE6uDyJCARiPs0029VEQF9Ea2bawusez9slLDizSiEUnjdFtNDchW7t1'\
  b'l7aq/xivuIjdVa5561LjVqoFKgE0NKdBQlsFqBxNXRnQTv3SfKt703MKgXDqHQX5dn/zZo5ewFAK8eAtiZRqiuIknSMmenapmM1QOhBR9fox0K3WGA6cye5S'\
  b'Vs2QVTZkYk9KTvA7cW3EcHlVx2Z/EPYDBQyxTXYVcyfYMjmr+eL1rw9Wp/RvfcfbPj4DznXO23/kjff8jX/yUy/Jplngqsux1sdH4mRgQ43E4whG9ff1sicN'\
  b'eULmRQwFK5e7utjfC0BceBTIZDGUqUWboCRp1ulPuk5CPBF1bRNztaCob2gFTXgJZva2ikN9IqgaDA6alpYALqouh2+T3bpJrJNQCw1ZYJ3dXPCballYlXuJ'\
  b'/EisHHxZ1rVr5FwNT2KF+zRPTAwgyJ0VvZII4EPTXTe0DO1a7YhOXAe6zEtuMT8IVg6ds5qpyzcsSxrvpMxF32QgUaqm5L8vTHX5GLLXk6rFhntUiK91FBlH'\
  b'gkqxSy0L99k4HlODswI5o5NJdYZcTcrx6NFtj+Fpfoanw4M4tzg5eHw8tU/1xgGExcxyd2OPtC0TKrLcpkSxneq5nYgJQuq8TQKQq+WIie8EUu7oaFHDqgBz'\
  b'qYRaDHdZvcjXcRphblbjKQj+iGHTNuq8jLVfxjGQmbQ6Hrui7gfT36XtTeciytZyBBsqByrtVNDNStXbpUrkBkKDzEpjg4+pzoUUviNx2HKDk+2lVUG3UsDE'\
  b'XdNUKr/2c8HDCYEWqdP6R09mpehL03yP4gpatEeiiW7G89FBIbyDSzYU1Vhhbm2VVTbl3wx4uPxe/d9TqJ4URGKJFvXr5JIZlus3HMrHkGlzqrdRNy6PYAqf'\
  b'7l5Znf7jGXA+hXOG1x+9CLyMTSzVrvMW7FymUbZbZdWNcztUxuYFgMjtKjiU0wkoEyqxqoCLKbpoJUoJY61gwqRlFLJNb2n5iEK+ULq9n2ZwGrpSnhN5FbPd'\
  b'/Eub3Wq/AeCZRQg8jQNpbVGqTWfQH02tWQtnhWZA5rYZNZucur0kaKsamzCHmg1sALLSBlVDsjqtCUw3pW5KVNsknW5TxtzuDhTC5IkZbE/ENCQxgnj91txZ'\
  b'8oevxo2HaiBDzREycDZqFZw6AW6eP2SkcbndgSiViGUka79SnzhTQwdlLFyPjKENVOQsZREWAnEr1eb0JxPJRL0UHj2+Zf3V3/m2izPgfArn53/kuz/y6v/1'\
  b'Jz9vhYGi/LuuK3CIMyHAy97p2LwaWyUme6MEFztbZWB7Y2QpdxvKLtrAAMJYp5I2mWIb/ZJanUUKpozrhf2Q6WyIykVZVcVcR+AGZmptnYjtjUEmgjU4uVvb'\
  b'qbpt4GNvLzi06HpCFERsi7SLxglJCVVfEpTbUaHtKyTG4vqb1dsT7iuc6oRo3i2EwoF0IXKBX6EwUaw8TGtr2gMnqxIDOdTSCkKl4n7RsZxxLPMmDJ2NJMco'\
  b'oEAAun2Blb5+g6jtcTKwToE4Hkp8T+V4wO5ooCLlmlOBYAUVNuvpmq1lpDOVG2Nd3ms+OGG/y93+yrXyxPGFj+MGOMPT5YHcMhw+9Inx7LPrnkyfRWRVglfY'\
  b'1PQsXuWUN3GyNstFgVGyblEvdYNYhJFLjKYtSDMGFoxSCF5lKdHizDWoA2SG11Eot9lSxZGxPQ5/4zROoPA86iTpRJffyuWUQNLeJKpa7vDoSfLrxUiTx+0E'\
  b'28zadjoZXqcp7LKEOkVxJodbzhJRUxs1UIgTomX4OBN3VsP4bpOfghiQ/bkNah2E2EJHQ8UkBJFoYoQFJ5U3oxTKJKpkj8UwQdpot1RNcerZVgQwg7SQxIJk'\
  b'IJNK9VKBhlK5gdTXnwEgm5qazJZIAM4ORuUv2YGqtvGq7Amrqv0+1SoP+Ipve+cHZ8D5NM7ZhX7osSzPzh5Dou2iRyj3nVRuDE3lbxhRiczuU1sVqiXShCAc'\
  b'6xNuG9gMjBo20c25MhHaWoN930R0HcAp1QzXaZRPprBhqj0JkNgEjwkvoT6WRmuzerUawjs6CGO5J10ZXRyKg4dxL62KsTdX4ITIt/HJF2c9Gs4JbVdu2uMy'\
  b'aXjMWCAOCqPmO9NFZXvSZbOF3YiYdEOuPsm1d8RvLo4U8l6a1oY877v+3BKjMH00biNv4gJY3j4NVp0srJIqrCMR+0StiIetImJA8migxC5AJKKiiq9Vk5vP'\
  b'a3eNNFqI8MDlOx/FDXKeNoDzEz/43Ud/9/t+/NFHxzO3aScFNY5hutkrlX9sF1/Vcvhkhds2ubrhn4KEwaklPrIyhMqosi4AlomWiT0pTs7Q8TzbqwnyKofq'\
  b'o9f25vZ4EZqOirUPyITLNjwa1j8u+sp0pHOfl92J9F0AZ+2qX8AUdtjqImRJ/3QNsbJpcQKwUAMi/3qGZG3CQ4GMYbtf9IR+6LX8sSEYghMBUzanG2mHKVAl'\
  b'rJ1EjigfImN834za3lNU+0n1z1YywGmVTQFmM3wnLj4+1kJVkKmjbo1xDCreShdSeYDS2r5GIZizjti6U12vRYTsKeP8Lq3OfmAGnL/AOT/IB54Y9W+2VwrN'\
  b'/UypKcs3LiDtTMHJ+Z16Jw0aDdPlZEEgnA2QpHyckIGN3fUYQQ3Mzdbiui1VHdubFWSb6MCmYkUhK3LtXKMOxDr7B/T5RbrtOQlcbAQma10oPE9wKb7xLylZ'\
  b'lVMtP4KwDUXYVkfYnWugZz9xkPZy1AsYeIY3+CQLnsJ+BEV5MzUrU5punPdT4klT2j8nneamghwFTVAkxg1gtLaLsLF4HUNQ428K2cw+vVKd/ozlglOBk871'\
  b'/7I4TJuWO4RgwhQuZrEbDykeuHrnwev+h58/mgHnL3D+7Q9+99HXf9+PP/ZYPntbqr5ToYMXBZgEomzgI03pGYyY4oSrSdq1exOyKQVLoSHtAquVDDNEM5iA'\
  b'0S52YgJGqa87Xbel4pAeYNeb+l24tGRdtvQkZD1iaZwAYZr8OAWpxur2I2LX1zRT+g6ZasnEyd7oZKJENjBKHq2rRNYOtYXF8goNbVwdbFR7k2805Wz4/n39'\
  b'wl2SzQaCdluicSrVa3/CbLvncLSVqtH72qsRH5PWaqaK/urEKbWf25YzKfBcvSQhTAQ7XwsKGqiguvZ77aa8tLf+KH9+7PCWG6a6edoBDgCcHfIHrozjVwoN'\
  b'rJrKGzWhhcVr4y8qeaYiEFtPUFMQV+Dpx9e0cRf07XQqS4ilhWKMkv2N45OU2oqQ4HpWQmUbPOQdxQlP2KLeyEgPylgCuvFnZyapPSeB8HUochvBntWrGISC'\
  b'RJsGhlIykCqgU9De2gMnPwfX56j/dHUMb2++1sC1iqYSzYq26oFARHubFmJv0BuJBhp9U0EMCjtSzfuaAiGmHQkdAY4m4M5hA7x9vFYtlf1cigAURJN6i0KS'\
  b'ZpdWtblX6g4AOrGa0LCoGypbUb927rty16Wv/s63XpwB5y/F5bzx6Ju+/8c/9Iic+/xqvK22lCgGOhRKTxFFpmrQRshFdukXPMSmQdZDu2PBJKJVaxyMNt6g'\
  b'fh/qYhm1bTlfD3C6FKv+LrXN4Em13+jxFM3J3U2noUYTHiPmtLc2jrqCoIy/k/88FSjIRrJMCYryv7dRnNq4u+qlavsQFbnciGBSblMtU1+XKRajn+kzEHQy'\
  b'cfLVgTBxR4jDx+hw9bZuTVeAE8IUiOLKy3iBF7fFqQESgYL/cACyCEraQV+TLQDdAq5P0Nx0yzxuqrl6cKlsiRiNjxO7gFd5iYvHF96HG+wMT8cH9ZY3/6OP'\
  b'ff33/Zu7rujerfXdWG2oqKq91ULeww1eRLoFQiF0jDFTvJEQJIu3YbTFrNErbL+fked4X2+Xijayiu2NYqsT061kTMAmpj86/Ex9cq+1Da1xlK1dlVNL++oh'\
  b'VLmv0h7VOzuXqJeqJTHQKW9WU9D6+JrawqKQT6GqXUgVALYpl415PcaGG7FMvXVFVBj3ZSlNI8f84ei1Wky0yrhvtdreVot17ckvDbocinnisZqbVjITF8au'\
  b'VRbLe4eY62QGxNYZnGSUtgKhAs9FQ5l2Zhlx35Xn/fnXfOdbjmbAeZLOL/zwG37zdd/3U189ys4OkQCJkSz9UEykVw2rhMqqgtU8xuuIGXZR0Kq0CY+nMYjY'\
  b'Iqc536GpeqtVQzNWo2m80HbAoZYA4GSz9vlbdY8nLhpsyVebvOGCgq8mhQaBYB3/+9hcw/TJVkF6I7vU1jVANuJdWMXDgA4+cdEg5S8Tq+SrChSmVqAm/Vcz'\
  b'idcKNl0LxYj9Hdn42d3yNvgbbGZ4T61zYl7vJDOq/3rYXBiFBvKYzNBMe5sTf/q5ez06Ok97K9RucVQL2BTPmwyV6jktwWa0AY5Gqw2vdBSPHl5Yv+r1//6D'\
  b'uAHP8HR+cGfp+L1P0PAqlDmSWSuwBUWqayVIgGwWBEm5+ItoGU2XN2BQ5WpbioQCWUqVk0Xg9rCyJYHVliIVEzMobJ1g+kTKt6213J2k69Opk5JEEc5Uh9J5'\
  b'vdjF37vscfD1bR4sraqhjhXxO6py2R+jsIQI05bUGZ15vZTlzMEqmaEbE8PMpuqIl3xjuny9Nl9q43JvTwh2k9CeUKataI5OJdy1uxRGW30AXWcBHfdGiLoJ'\
  b'2nSi1jVlnQyozwOX+LrVVZRgIVI+QIqdiI6QvAZ0XQy4dATpaGbxFXyyezq7iRoBJ+MCH79y52/jBj38dH5wb3nz/3jpDI5+WzVdpaukAAAgAElEQVTnLIUM'\
  b'Lr4vRS0sUiqQ0X6fzWw6Z8VaBKtRsM4ZY84Ys2AcBVkEowjEflVRBx0xIBNUy4pqTdFaIN1w0N5e4dSOhW1ERUwbXjaTtcJQBaEZt0e6WKekKcIEpAn4mrez'\
  b'rSt0DQmHcLXWYml1q9P6K0FckFbFaQOUEhQLCBIUA4ABigWABYgWICyKvkSrzmQAUVHixmpJnU2pkyB4deStmluGBM+ZetluCKFqFHCM5KWOJPZd0C6gjrYq'\
  b'LTYy70Isr6qYiXn5TzzvS1oUr/XuKi0PTHUEZA3RESoraAUd83FWktJeaTVLK5WQoO1S/dfHn/vhr3rDz1+cAeev6Lzth77zkV1ZvZdUckkcLBoaMfOobMbS'\
  b'uYLOKAY6BVQkawGbLFhnwWrMWI8Zq7F8zGiAk+sLbSCmvvYfbXHrBYbrmiFxIGtjwIHnDYFCEkQEGg2Jn5NUxvBOIKKukioTfgrROgiAU0lnDUZdEiYpYpUJ'\
  b'fI+nqovL+DcFcVsqkTMYbEze/l1r21Wja+rXUupAU62qo7pM6mGDFNJFdct2dGxPFO2Jqv8LuijM1kCjUyDHqVCMFe7I5djKwHbCxFqi3D8ezYXYja2Q5hLr'\
  b'IyMgY0nKlDUgK2heQcdjaD4B5ASqI9Q+htwkPRsQiV0T5et/5OJdj37F63/hHtzAh2+EB3n3m7/9sZTX/zmrjllKGNyY1dogheQSgpalgU22imed28dmKdVP'\
  b'/X/MufvYnMWd80SKlagGZzwJAW6fesCONpK2/o1I8JjR3mN3Im7UCXiQ72k1IWSrZKrlZbDDqhUPV88Ycc8gVS3RM2qZU26cXoyfPP7Yqp6W5237PALnvqqr'\
  b'n2L6xhMosvMTZHft1jJIyzmv6aDOZ4zh7+zvxd70ETC0VgajtyNa7VlDe1LApb6p6xtb/OurZktGyA00NHAsKJWNP3YZ/edsj7P8qnkN1fI/ZFXAJZ9AcwWb'\
  b'lYHN2iqgseSR6WhTVgDSFjTvv/Ssgy/5h7/4W7jBz3CjPNB3vvn1F//uP/vpX1/L8EUCPU0sZTJiFTdbI61E5X0h6g6A0eaEpibZRgwDZWu7JnFWP5pq7KQV'\
  b'fKSWytfxGK0ktUg/qSCdlPCKqft+NdNCXJCkOjKFg03H19Q+wVXE1Il4KSRHdmNbNS6HmuGXdv7DfSvSWg0JdIqR9KTl9UDzkeksRmosD8WWZ+IArjRldTf4'\
  b'F0JIuZwe0cZPaR+IFydduiGE0c02qgJpXLXXzbUKD/tDI3YrF9PI4AJEmkcDoHUBm7xqIGUVdgGsAjwEhUjGRx5/zqNf8s03PtjcUIADAHf/H99y9XXf9xPv'\
  b'Ae++lAd6gWs+DGhcpqFFxDequZZQPyHtLr26FCgGOhVcVI3HUWvXrN2yN6t+kqoGGpWjMV2gl+JTRxpQsLxUt5rQCD6dHSgFC87mPVxbrPrOEt8xIv/ZShxJ'\
  b'+JqR3wh7SWpZ5u2xm0VZ9Un2VQRui6H1Z6YmCkQ0+op8Lm0upmmnXoZVVhJWBnqLjamac7rJEMVJ6kAb5Q32nTbuIVJ29hAielDb0L5abW1bLu0RNeApLZUB'\
  b'iVdiVgnpCBLbILeKCTbFOhkJH7n4vD9/1bfe/UHcJGe40R7wL/3wt68AfPBrf+Bn/zyzvGxgvgVms8AGHoSyfsIARt8svwZEGJ+hFp8CoLRS1jpVsBEtf19B'\
  b'6HqGOAyGUAucU+cBLBsKIdIFzaGPoCGpoIFQDfajqv8IawruuxMMsoIKzQR60o/lS/ZE4Voqgcyl/SAz+K6xt6jPSzX+pkDMYpLbTYCEkXFdhJWwlqFWRZFr'\
  b'okKFht66Iupdml1qdADskzwnOaDBREu323d0hY+274mJMFTUQ+qks9gRb7HdcaASwDWVAQLNpWUq7dhoSSAVdGLLuIbIGqQZjx6cWd9/5Vm//be+7Wcu4iY6'\
  b'w436wN/1Q9/8BID3fO0P/NztmvhzE+NCDWtL1oZIkXZABGFao914lGrMr7ZLs7ZUNZdJNUb+lvF5VrlOfWMJnp50OfWfoH4XihDibetNmybOgNoWuKqzXvCx'\
  b'cSUubWYreWeTdbKDZRlJNU+7/jlUAkQSpkJTV7zG0UrkmmqVIxr8jUNrJH3L1MGG52rFKnC6pkk9SGBT0V2dDKtpPnRzIuj4BfKgv/5G1Gegx1jhWvFpiPRs'\
  b'3BAmnE9QFSNbBSMONpLXIGSIjrh6stT7nrj1z778W952D27CM9zoP8C7fugfPALgkb/zz9+6n0WeD8KdAM6UdkuRvFwXZEySAqo3cL2oVMxGMoCOKEYDkJwr'\
  b'gSz4JD1VsRONoORWPl2ASYhsbXoS6i363MDcWQmmEItcAYZd2s+0qXMW7X/myDlQJXY7CVsxAypAnOGWmqgJGCGy1qdyHPa2wjpAzMmaTO66UXTHunDL0p5w'\
  b'OnW65s5/1dERE45u2lKFrK1ofyFbeJr2FEl74rTtMZGSG9zHFszTXV3op87lKKS8/pJBsNZLR1w9YX3icO/w4uH+Pa99/c99HDfxGW6WH+Qd//wbDwDcA+Ce'\
  b'v/lPf2qxM6TzA/NtQlgAOFvoDYJubku2Elq0OT6oOlejNsWyEDy1yufw2pwxFW9aE/coqbV61O7mKk3gNuWO45IzqM/Y8uwt9gqnLWjTBGQ0bB9LaDVCZK0H'\
  b'5JEBjLYlxkC+HpwMerhKuXi5i+1NqVLxa1YmlURZhwRhUiEGEqlSYq22sO4VbX+qQXQUN7o7/xg0u88aRxyDq5pTTit1fLM+ViWTxRXpt0l8Zc5aZTEWXUTJ'\
  b'NmjKwKhUbCSFyyMokXSVUMnTdCW5a4hgFrcMqNI6p3x0MlxdjXx4tB7+/Cu/9Scv4hlyCPN50s/VP/26rxMDm0ovVnKxCfoCyoRbsadYVguNQBQ3UjiYjEVP'\
  b'YvSDGei0RZS2HFiJdsQUgmRfz5zsTHwnqvjIQ6cvvuKr/p/3zK/ufOYK52l22ojZtthjBniYw2zOuprxlAaeoZKyFEx4YhfA3NqXrpJw3ydyLqerHFShxUW+'\
  b'+ZO7laKYB5E5Ll5PBjCf+cyA89SWjaL9n3ulKzqTqGo50fKkmq1qB2GermAOiCFTnGK2k8aVH/KpE5t/kMRAPAcZI4yVQZSLvi74xWRZf3qg21ve3Uj3ChDR'\
  b'DK4z4NxIV616qqfGYbQKpnZSrtfpAKuFstXRMk3/o7b8UIWA7jmszRFOJwyqultcFRjC7S+rFeaYLbWiLrqKQPL46YEudWup85nPDDh/ZRWOFjvUqs2I9jfT'\
  b'HSylxrWwJ0kiRNkGL6gwx/EWqo7ep0IU3XLrjlYHShPi2tYAiA2MSiTJmMU5qPnMZwacp21PJXHwvQUEtDPOQlxniPahXvMELofQ5Ugq1EP2+haMnCRmaisB'\
  b'FOuuLuKSIDn71xEpDohlZy3Plcp8npS3xnye5HPlj//7r6tCQZiWp3Qrob0JZU+tcjhYVlaCuKl4w4Kmj8Ank6kIbF4pmZZF2pSqjutFJORemT7GvJzzmB10'\
  b'JAseu3puffl473JMihBbf6euCkMVzelULd2rnWtbN11E0LZypqGe68o9T3DYiA+E6rX7ONdbFdBey+LgVd/yrg/NV+xc4dzwLE531dPkDdEnksSGp7m3BIWx'\
  b'gn2tf1NRa1tFQTmtTRRidhAS3tjqLRzVGGP7LuKjezjY1Hf9hf2Li3O7cisA5JzLQzONUa2CqEtKnfgzA0GdO6n49NrZLhqjYSZrCBFcqFtfmEoOulRB+z3j'\
  b'0smFFYAZcGbAudHhZjONQaf2W26QTWGjGx7FS0Hmz1W9i2Cu5ZEr6Ay8K5DUyoammdlg52cwMWmnqcUhtPg+YwAvL2BI5wBOYN6FKFmWeTHZci9etNievpSO'\
  b'nr/xidgGpbYcStGkSxBjn3uQjt+P22uw9T5QgGg8/AQev/+e+WKdAefmKHB8M0vq+kC1RUXbm7IP5rBJ3e7mbW+q8/hW7bLlmt2FTmwcyFZSpz6pwcM3zOhj'\
  b'BpLkpkpOy/PYPf/5GPbuBKf98rE8hO8bY1Jiol2ykos33uyt7KNm1H6NIrF8aNr8/dT+07PYNz920yuNcPCJ34LKPTOlMAPOTUCMEbmPTgMFbUI80PYEzVrb'\
  b'kHaVSUwRLW8gwoasODr9TSoqxDyvGMQW/JI1554QUQVRws6ZF2Fn/84CCnIExQDKYyCdpSQ8+FnYr+tWlVAqi4rVJXAbwVIBquUTTNY9EophluVuV6N3tzq3'\
  b'n1gADaBD20ALwHh4/3yhzoBzE1E4ExBpFUjfa2nwt6EQ0FZWiCjaUzmYhQQYBIutnssIbzh1sKsEbms61Ksm9qXDBmCptHNyslF5lDC8ZP4t8TI6CuBhb3SN'\
  b'PhDV22bogUZXwSBoKAAFa9V42BoYWNM+yb9Ofa7XsbltFZR9r3zyGNZX75sFQjPg3ExVzmRvigp5i84MjILXr4bFzLaUWTsXVgRfnF5Xo2g+x9BJ4+Lj8Jpt'\
  b'Jc4xUbDFEMnwNUifnlEJZchXAoClluCgseIaASwDFRNzx1OoxKwC0jW67Jb4eZTsga/RvC8WZU9ELQHUP2HVu3x4tcUbbVT5hcGcIOuD6/pSz2cGnBuvykFw'\
  b'/KvVTMUJr1JquC27cph0Mg4Hgg2FBjJVuyqmopMaOGmdUlFxK6Q6+RLt/cox5VeMy5ETnFz5GPjM88HDnlUIJ+baV50I2SJhuFQ3GviaalehMJAoABFrr9bq'\
  b'tK3qBgSpAJSuC6DVv5aWMgZKxeDdT1jBcLKLOtCT9VXI6jIIu/N1OgPOTYA1VsmwGUDFREy3owzpCxP3iVANkH+exspnI2qYgt+CaX4qovVhSk2VjFIw1G+a'\
  b'mJBz37eQKlbHjwGyws6p25EWe0bKMkALM9tYBJAZLbWzRvUuW/qCqmdPFZBI7l7Y/TAW0FeAaA3ocfl+ujJYHVpbBrOtiBqeyCcZX6S1lTNyW04uYhyvQmfA'\
  b'mQHnZjhMbLoU2+TOW/x2fSWhBsCVNz5P7swtG7yZ5BT+pLQazEHf0rO+G5xO4IN7HLLPZabyuO0xMzMkC9Ynl5HXhxiW+1jsnEYa9sC8a1/gCE2AmEqgm1Zy'\
  b'+MjIXvVwPFCyxMlY5QzFYtMnUKEycXK4VjUDFLnklhNZpcitoqlLpsq2lAob3XPxDKYFxuNH8Uni4eczA86NdRIzcs7h9xLu9tZqEbUdqtBqkWqnW+vc/SqQ'\
  b'CIKh1aS1CtBWCet+Gl2ALQcXwDpBSxUsmT3sDQCyjMhHT2B9fAXDYheLnR0Miz0wL/qWRSVUGSm0Tcl1N26HpbUSGnzHwy07awXk+pvBHd7ZUzATakOqncDH'\
  b'wvVgrVzlgpQAnCAfPXxtj+v5zIBzw7VUVXtTlzGh4FRC4bKIczEcsmtIqxm6/RXYDbiomdkh7JL3mh7d3NaMaQJuyxk7F4/K1e7TEzGyZhCRA1BFP8GI1ckB'\
  b'VqsDLBdLLJYLpLQEp4UBTbUgXRS+hpYGGovQCqVav7WOUBOqB3DJwcr9/I0GEJf/S/VEAA8gJSiVCOj2fbRUNCENorgKMkRHrI4udsmm85kB54Y+lBhi1U03'\
  b'JULxOq7EbzXHmsYzkY+ug9o4kDwa2qC2Jl7Vthpyn/pRebX0bPlOk8dNZOsMVKOwQKksW4gE13EU642TkxOsVyukdITFsMAwDOCUApAaD0ME6Elrl8z/WJFK'\
  b'RhMIkkfkvIaMa0vJkLChTmAeQJyQ0i7SYg887IBp8HE4UYIigWhV/JC9mKwgZMus+QTr40fnfmoGnJsIcND4DyX4G7COnksEbqE32EOzuBHCtoIQ7SfgCRAW'\
  b'VqfdGlYgiGNrNtX2UvxSxh+Zw5+rkoP9hT0W5qYWFs3d6F1UIGvBuB5BzFgsBqSUkDg1L5/gtUPgkpWtKBnw4woyCvJYMpoEsHzlnvNy32ZOGIYdDMMehuUp'\
  b'DMMOKC2s2mGrDsvPpFyBZ+W89vrwEvLqKJpGz2cGnBsfcggCNv/hRiCXVqKYm5ulBFBaBQTxH1NHLMNI4lIdwUnYTpjry56lRSJfpCom5Z56SSF/aZpUYHyS'\
  b'iNpjLbtUhbtlaM4FpBitgkOQ7ojg5PjEie6afMruuww3OB9zRh7HjTYQUOzc+qXYuf3LMZz9XP/6q0ffi9XjH8DJY+/FahyxpgMMqyUWy1NY7pwCD0swBmur'\
  b'BihGkOyYOHE0OUDGycFDk6SF+cyAc1Mck90rwJTsBl9qiGTAUxMmSxlke0AaeRtt4KOTdSQOSKExmSlUSDUjKVRARTNTwEtrRncSsJSInJb0UD6LE7ewA1tL'\
  b'zyKWl244of0yqMVaIIfHs7ExThOkUWDvOX8bZz/3n4KGUxvP5vL85xfy+vgTuPInP4qjB38F69Ux8riGrI+x3Flisdi1CmewqmplU63yPcfVMY6uPtYe68zh'\
  b'zIBzc7DG6hVKFf3VCQ1b5EmxBuViPcFVQMdegZA579WqpQ2yKgiZApe15WsBIYCloJJHxNR1BrVoGFj2U9TZWVxyCWsLeVndu7NsiUPVWkS0yovMpiIagSmw'\
  b'1Wu0Q88Bt/2Nt2DYf+EnfWrT7h04/wU/gp3bvwyXfv8HICI4Xh1DZIRIxmIYwFxH4wTiAuIiKxwfHEDyaIJm7eN35jMDzo0PPMaT1D0m4sAzJBCnljgZFbfV'\
  b'T5ip65pIJ5FwBAAZ4AJsXM20ql1oJa1JWxVE1nJJtu9jq48sxTdHqpG6JT1U8FICU7IxflEZMxeHQLctVMu20TDmZ7VirE7otDOQZ17g1i/5OaT9z/q0ntq9'\
  b'53wtFmc+G4++5xsBVaxXa4gqZLnEIqXGf+URqoKTkxWOjg7d/qNXNM9nBpybgMeJzCezlfaUXEMCGHfDySocKtqUYKHXpXTGMAFz9CMMlsGtnYpYqSl8fYHT'\
  b'Z1VlbFy2HAQgAUuJjGEfTQuIFSJFyGc5k52KmULLJa6uTgD1AYNiJHCd0EVEvvDX/vWnDTZ+8Z55Cc79N/8bLt/zf0FUkddrHOURYxpAXE3LCFky1qt1MKCf'\
  b'VTgz4NyUbVX1suHyJmW2ysY8YEwQV7qLqi1h159Q7KWCe51GgtjJ4cDVEJrLH4WWiIIimRWq2XauLBNbGMRaCFcIRBWcbLqmLe62VlYqhGoPwSZkrN8yZ2l2'\
  b'Wszh8ZOTzrt3fg0W5z7/L/U0773gH+DgvrtBV//Mo5nXOjZivQI2E0jIMryqonvGnRlwbpJWqv0mhZC6sgmtGGwMPnhlQwY4ZJOr9rXIM8PdfzhMdEuxYW9u'\
  b'a3VQc6e6VNzmsUzWVqnYWgUEYLG1AymPgTKYBCLGLWn5GMoCJbH1hcom1y12dQyso3QOKxeizUOZOeH0S970pDzdZz/nTbj0/n/slh7V8oPD1E5VoaxQLaR5'\
  b'zooJcz2fGXBuVLyxFkgttA7V1Y5ta7r+aouOVBcezdG426Xqs6Xcy8aqFNL2hnLOZuJrU1cFalumZgdKPNiEqYzpIQJwLo+NxpI8wQLVsQjoJENZAGSz2mhL'\
  b'qAopal40sV7b72qVjljlw7vPBu8860l5vpe3v8qna0Rh2z5m5/i/m6KbFUwziTMDzk1wHn58f/2s8weL8ka0jG7zeREQiIayLQ0GU5H8i1LR42i1daDe68aT'\
  b'HtTv2iHtDkSt3aEQhqX2+7qfRXW1om5bExcgsQUtIgaJQjlBKRdyWUoapyY2UjlBaQSpDb5JbGZuhHNUPIcWU0w5rAosbvnSJ/U5T/svxnj1w0ic3CGxxiOX'\
  b'/EEjuYWRKsnOc0s1A85N1FF1Cl+itsQYKh5RBnEC06IQyg5OE3KTWnukUKswgpuXahmRqwTfHGuhnGAWaKpfR0oFQ1y+JwFsUyolMW6HzaA8FV5HE8CjOQMS'\
  b'IFXLkwsgmbyZZGqJUfgg/1sRpOWFJ/dCvvByrC79sW/ok2mfakRNZcVSYggJkjKy7l2dr9YZcG7488SV5aVnnb16G6cCHm7XYGZVFWiYB3PAG6D2P/NgzEct'\
  b'YDgATo1/iSBTv6u0RUw3SzfuIkTDFICyzXVWlLF6sspHgaQgkbbCQGxtFkElF9DRDM1r33gnaUanqgLlsM9V443rtEw07Gw9eYeGsxDJtktuoJzq6JtBJODE'\
  b'xQWDAGYFMZ/MV+sMODf8WY98AMJtYlvjPFTzJzaxnVUO1R+GFsXQigvoVLFaEfmFnCf3TtcWdFd5GxFXH5P7Vxg3owAoBw8u8X8vQFUqlCLkM3JYuJjtmF6H'\
  b'JEFoBJDdIwuZwWqeNGU2FQzcPaPGrDnMGwgC+StoZI4e+R2IKIgySJJlu0t5LitwkyJVE0JRCBYPzVfrDDg3/Dk6Ge5T4AUeD5PNI8amRcRDGX8rFz8ZKsuH'\
  b'xbc3GShF+3Qyez7yNM1gbFze4DwRBaKK9uDqYviUyoLwavskuWhxKJfkTQjApYUqquMMUAYngmbjfTJB2TbXtYyc1Sqsmk1et9Mrl1JihMtIfHXx/U/qc766'\
  b'/NES5Ge7WyVlNEFVSoxgtHQlQhoYQ6JH5qv1M3t4fgqe/POab/h/Lz5+ZTdXnXDjYQhq6wYiZOBSuR0DG9fmVO7Efl+nWjZaL4ZUQ/s9DLjs75QWIPuaxIvi'\
  b'S8P1/wWIllBegmgJ8A5ASxDtALwE8Q4o2d+lBYgXUAu9Ay9K28KDWUYsDEDNq4bLYyAyrxoPpuvdDg8f/e0n7fkeDx/EePiQxxmXmOXKm7X/yTbyEzNG3Xni'\
  b'5d/4K6v5ap0B56Y4j13af8KD7ShaPlTbTDLF7lA2mu1/8uTKVPgRCkFzFCw3qf6f2u9hb3DjiZRN8+Nf30DKfi1Vl/FIBkTMu+X3tASlJRQL+/slwENJ2+TB'\
  b'SNk6bVuAaGGPv+erNKRrMnNpZaSQ3gf3/4cn5bm+/JGfL081sbVrFgEo0kd9EsJzv3hwvkpnwLl5iOPD0++vrU0UAlJDHgMhDUF4QLBCL4ZRWr1k7PfK/mfE'\
  b'P3skLhvoVJ6oTqHSBKSa0rmCh9Ky8Em8sKpoAUpL/30BqoVVNcXhr1Y6pZJalv8pATxAKYG5cFYqZuJV/0wDHv+DH/5LP8+yvoIn/vTfYnn6LqsO2RwXq67J'\
  b'jL6Kh6JVOWlUSh+br9IZcG6a89qv/1dH9z587gDaOw23+622xc26ixSl+JMkA6VoGIrJV6PJ/w28OkNyB6UwnnfRYWpghGSVzwCiBYiXZvFZwKS0VsmyxZN5'\
  b'31T7TwbR0v68cBKcU5UDFIAddp+FnVu+EBc/9C//Us/zQ7/1P2H33Asx7NwGVm0m9Er2WKnbSyMGBPzQc1/3a+v5Kp0B56Y6Dz+x+4HV2Ie9tWrGtrEtRqaI'\
  b'f8W2ty1TSvtlTYT1qS1BlAFqjDvyLzwBHQrtBTVNUFtuLG1XqWxKJcS8NE/hUvEwWStIpb0q0zT2qge8RKIFOBXQYhowpGQgAMh4Fcszz4OePIwrH33rXwxs'\
  b'3vs9GA//HMszzyscDuCPt0yn2MbkRetkleFalP54vjpnwLnpzmu+4V9fvPeRs4+hrhI4WtQEh/Irqbh9ROE4xMRqre2adGIONIrtIOT4AtrY0A4sNjyviZvg'\
  b'sCFX3fdKZaOdBmuVjCRORhxTaa84JTc6r1xPMqACD875MA/QfITjR9+HnbPPw/En/iMe+d3vh4wHn9LzOh4+iPve/bexeuwD2L/1ZVgfPIDx5JIDHzuBPYCS'\
  b'2YAoFddC8Edv/6rfPJyvzhlwbsrziq/6N7/5+JWluOYFtrNkIFNMvrMtV5YNb7VqpwBJW3isBuiYVj7a4oI1IFBd1vRCR7cAk0bXQAMdrftcZgzGldCuFYpZ'\
  b'a1ABolrVUNUQVYCxKRYb3wMawGkB5gGJGOur9+P44h/g1C0vBefLuP/dr8Pjf/SvMB4+eE2gufiHP4p7f+WrwHqCM3d8IWR9BSeX7y0tKheZAVtrVzbw2exY'\
  b'Gap8+fxr3zNXN0/hmXU4n4Hz0U+c+a3Te5e+bLGQIsCz7WvVonFRZBByIVS1GK+LUktviFYXlQES7ZwrYt9WLXtdj6ex6LEJjk58k63V8vjeQsnahMyEdMTm'\
  b'/F7NwOwbsRmxc/IVLWAoxmAoES1cF8th5u6sYMlYPf4hQDL2zr8Ew+5ZHD/ya/j4x94CHk4DvNtysQ4fhMoKi51zOPOsL8DO/h1YHz2Ko4t/DOR1ianhSlyb'\
  b'iTtzc0dUHZX49+ar8ak9837+Z+j8xju/666Xv/DyKwFLGKChjJ55CdAOwDtGypZlTjUHQCJuhukt+S56XH3aL7i7/JFMUh8kkk3h70z8p9mFgCpjMSfXsWyT'\
  b'57GkZyLbODobqI6AjCVnKo9QWSPnNfK4Qs4jch6hyliefg52z70Yw/Is8voyxpMryCeXkPNxmS8NO0iLPQzLM4BkHF26F4dXHgBk9MomscXUpAGJU1EVczG0'\
  b'l4z/cuG1v/rx+UqcAecZc973y//oFS993uHztArjaNFAh3ft90MT+Rm7S8ydmVQ0l9LgCdjifScvreoEcaZMEBxgfA9JpZHWZmhVqzGolDypACak66JSpmJj'\
  b'IQZQ9X9VgeQ1IBlZ1oCsMY5r5PUKWTJUFDzsYbF3GxZ7t2NYniqjd11Zi5mRxwOsDh7B8cFFyHhY9lRTAqcFEiXwsEByDqd44xApRPT3b3nNuz82X4Ez4Dzj'\
  b'zn/55e9+xWc///B5hQ9ZQGkA8Q5AOwY8SxfR1f2rmjFO1I/K4bnazQtngyS+FsCAJn+OsTEVeOK+VV2NyFCM5fcyQnUdfs2WLS7unwNkiIyFf5JS8YiMQB6R'\
  b'xxOICLKMkLy2SBqBSgKlRbHuoGpTuoaO6/K5dT3BOKRS3TA41SC+Yp/KpBgzfv/W1/zSDDYz4Dxzz2++803P/dznH3zhziIVwDGlbwGcHcDUva46dgIXJZ5l'\
  b'CiLUg0y3hzWdUFHIGqcpeGkPPJAQQSNlW9wqnmLyVcLrSGuLtS5/J62qqW0YKaAyQiWX9sqqIMlraB4NdEarhLKJ96rXjtVdROaZbODLQ2ujuKxbJDIPHGtV'\
  b'lOEAAAV3SURBVMU4Srrn9le/46PzFTcDzjP+/Pq//55bP+vO479+/jQYlGynyaob3rG9qOR2Fj6yRo0Cng4YdePl7AjhiFBdRHAn8em5nFrVEJW2ySqdwgFl'\
  b'I70FQAESyLrwPZpL1QOrgiqvAw1cjxp/U75G/X0BnZJ1VfOtatWmlMx0nku7ZNOzlJIvbRITsuBAlN9/+6t/4fH5SpsBZz7hfPBX3/hlL7xzfQuqyI53GrdD'\
  b'cUGzTo7MbsHahpbvtA12LGp3kiCxvbHS9nk1yypkUhUL0Wa4XjgeCRxNNhI5/CqxyrEKyaZzRfRon5eL/46qQLO67Wlb+7BQQLPCKLlYlu5Zp1H2w4yZ7ltn'\
  b'/sNnv/bt82LmDDjz2Xbe+x/e9KLn3p5feu40cdG2LH1nqd8KZ0/lhFtYwKudNh1vbnvT0ThxWCINVQ9NKZ5g2tViZsQqjgYKcNCxaqUDHAl/l63iaZ8nko3r'\
  b'UaucxFoua+FqSoUEdTZgDomNOrd4nZNVpt+749Vv+8R8Rc2AM59P4fzeu9/0xc+7I9+xMwxQrlMsWzEIW9iV06mtlUcGh4Q8iqmXNNnNmsyxgqa5TbtUtrdZ'\
  b'XiKVioRq64OmKYKM/b97m2VgJaO1chWs1Nc8UONt6mO3dM8KeWyeQKTuVzyOgntv/fK3f2i+gmbAmc+ny+2845+cunB6fOVzb5cLw6LuNFVLieS+x+TAw13K'\
  b'Q4WbVtr0fVb5WI3mNBtUUHEJJFNBU0vxRG2pmn0pvP2xJAexhAdrl0obNnorhcADwWxMFblVVFLBTduiqlilRRyyuTDmLPfqsPOnt37pz8zt0ww48/nLAc//'\
  b'fOrCfn7lHbfQ+b1dolLpBN8cN1yvyuBKosaXlTbAR1sj0mt3PBmiAIwbuIfkzkYma7Mo1bquUcGkVixj+bOIA1AVA7Y9Mm2j98oJgUql5IEULQAQCgjJsYo+'\
  b'kLH48Aw0M+DM56/g/M4v/+OX3XomP/f2C7zswWYwb65i3FWjhMsLWxM82d68ZO4QXMuYSZsF267uvXlaK4UwIo/jc9PmqIkFq2YHeQNsqPI4Pn0KlYw2zU/b'\
  b'krfWqoDUIypy39kvfev98xUxA858PgPnPe/4nltP79FLbj+vt53ZB5dN7iYOrHYT7Lal5EDSRuEwfQ+FGBUKpDJ1+OKrED5iF9uPsj0r80WGiI3NIxeTvZoh'\
  b'qX/Wrrohqa1T81xWb9ny4yp6PzE/cPqL5mpmBpz5PGXnN+7+nlv3d/GCc6fys86f5eViaNOrqlAmrkBTo1Par/BqqCBNiealDpjM+SuQO7G96Q3ZY4WCMD5H'\
  b'96sGe47cR9fAlIGQhyH5UXD6+P5/99MzyMyAM5+n4/nP73zTi/d39Nln9/XMhTM6UPVAVjTzqc5JkNxrGGjq5MLdmLCwmoHBInw9uhjNEsOjaKQDIu1AZrIi'\
  b'ASOKFSPJ+LhAHyPJj+x/8c/NQr0ZcOZzQwLQO9744t0l3XZmL58/vSeLvR2lRClkSFFT6igAJmu1NIAPb65GmFMhhTxzMw4NJmOVh2kb57YQ+jhUjhT5soo8'\
  b'cvqLfnYGmBlw5nPzgtB3v3hnqbcOhMWZfT27ZOUzpzKXDqpOvCyrPNWQO+1XH9QKo5iJpS16WCEjqV5V1bXK+jKpXhLowZkZXOYzA858HIx+4bvuSoxdItW9'\
  b'Hb1lkSSlRJqgyqzCLJoYmkiVGcqAisoTibACFExy9fyX/PSc1T2f+cxnPvOZz3zmM5/5zGc+85nPfOYzn/nMZz7zmc985jOf+cxnPvOZz3zmM5/5zGc+85nP'\
  b'fOYzn/nMZz7zmc985jOf+cxnPvOZz3zmM5/5zGc+85nPfOYzn/nMZz7zmc985jOf+cxnPvX8/8AOpnPP9fYzAAAAAElFTkSuQmCC'
  