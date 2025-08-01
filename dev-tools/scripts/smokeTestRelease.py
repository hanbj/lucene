#!/usr/bin/env python3
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import codecs
import datetime
import filecmp
import glob
import hashlib
import http.client
import os
import platform
import re
import shutil
import subprocess
import sys
import textwrap
import traceback
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from collections import namedtuple
from collections.abc import Callable
from pathlib import Path
from typing import Any

import scriptutil

BASE_JAVA_VERSION = "24"

# This tool expects to find /lucene off the base URL.  You
# must have a working gpg, tar, unzip in your path.  This has been
# tested on Linux and on Cygwin under Windows 7.

cygwin = platform.system().lower().startswith("cygwin")
cygwinWindowsRoot = os.popen("cygpath -w /").read().strip().replace("\\", "/") if cygwin else ""


def unshortenURL(url: str):
  parsed = urllib.parse.urlparse(url)
  if parsed[0] in ("http", "https"):
    h = http.client.HTTPConnection(parsed.netloc)
    h.request("HEAD", parsed.path)
    response = h.getresponse()
    if int(response.status / 100) == 3 and response.getheader("Location"):
      return str(response.getheader("Location"))
  return url


# TODO
#   - make sure jars exist inside bin release
#   - make sure docs exist

reHREF = re.compile('<a href="(.*?)">(.*?)</a>')

# Set to False to avoid re-downloading the packages...
FORCE_CLEAN = True


def getHREFs(urlString: str):
  # Deref any redirects
  while True:
    url = urllib.parse.urlparse(urlString)
    if url.scheme == "http":
      h = http.client.HTTPConnection(url.netloc)
    elif url.scheme == "https":
      h = http.client.HTTPSConnection(url.netloc)
    else:
      raise RuntimeError("Unknown protocol: %s" % url.scheme)
    h.request("HEAD", url.path)
    r = h.getresponse()
    newLoc = r.getheader("location")
    if newLoc is not None:
      urlString = newLoc
    else:
      break

  links: list[tuple[str, str]] = []
  try:
    html = load(urlString)
  except:
    print("\nFAILED to open url %s" % urlString)
    traceback.print_exc()
    raise

  for subUrl, text in reHREF.findall(html):
    fullURL = urllib.parse.urljoin(urlString, subUrl)
    links.append((text, fullURL))
  return links


def load(urlString: str):
  try:
    content = urllib.request.urlopen(urlString).read().decode("utf-8")
  except Exception as e:
    print("Retrying download of url %s after exception: %s" % (urlString, e))
    content = urllib.request.urlopen(urlString).read().decode("utf-8")
  return content


def noJavaPackageClasses(desc: str, file: str):
  with zipfile.ZipFile(file) as z2:
    for name2 in z2.namelist():
      if name2.endswith(".class") and (name2.startswith("java/") or name2.startswith("javax/")):
        raise RuntimeError('%s contains sheisty class "%s"' % (desc, name2))


def decodeUTF8(bytes: bytes):
  return codecs.getdecoder("UTF-8")(bytes)[0]


MANIFEST_FILE_NAME = "META-INF/MANIFEST.MF"
NOTICE_FILE_NAME = "META-INF/NOTICE.txt"
LICENSE_FILE_NAME = "META-INF/LICENSE.txt"


def checkJARMetaData(desc: str, jarFile: str, gitRevision: str, version: str):
  with zipfile.ZipFile(jarFile, "r") as z:
    for name in (MANIFEST_FILE_NAME, NOTICE_FILE_NAME, LICENSE_FILE_NAME):
      try:
        # The Python docs state a KeyError is raised ... so this None
        # check is just defensive:
        if z.getinfo(name) is None:
          raise RuntimeError("%s is missing %s" % (desc, name))
      except KeyError:
        raise RuntimeError("%s is missing %s" % (desc, name))

    s = decodeUTF8(z.read(MANIFEST_FILE_NAME))

    for verify in (
      "Specification-Vendor: The Apache Software Foundation",
      "Implementation-Vendor: The Apache Software Foundation",
      "Specification-Title: Lucene Search Engine:",
      "Implementation-Title: org.apache.lucene",
      "X-Compile-Source-JDK: %s" % BASE_JAVA_VERSION,
      "X-Compile-Target-JDK: %s" % BASE_JAVA_VERSION,
      "Specification-Version: %s" % version,
      re.compile("X-Build-JDK: %s[. ]" % re.escape(BASE_JAVA_VERSION)),
      "Extension-Name: org.apache.lucene",
    ):
      if type(verify) is not tuple:
        verify = (verify,)
      for x in verify:
        if (isinstance(x, re.Pattern) and x.search(s)) or (isinstance(x, str) and s.find(x) != -1):
          break
      else:
        if len(verify) == 1:
          raise RuntimeError('%s is missing "%s" inside its META-INF/MANIFEST.MF: %s' % (desc, verify[0], s))
        raise RuntimeError('%s is missing one of "%s" inside its META-INF/MANIFEST.MF: %s' % (desc, verify, s))

    if gitRevision != "skip":
      # Make sure this matches the version and git revision we think we are releasing:
      match = re.search("Implementation-Version: (.+\r\n .+)", s, re.MULTILINE)
      if match:
        implLine = match.group(1).replace("\r\n ", "")
        verifyRevision = "%s %s" % (version, gitRevision)
        if implLine.find(verifyRevision) == -1:
          raise RuntimeError('%s is missing "%s" inside its META-INF/MANIFEST.MF (wrong git revision?)' % (desc, verifyRevision))
      else:
        raise RuntimeError("%s is missing Implementation-Version inside its META-INF/MANIFEST.MF" % desc)

    notice = decodeUTF8(z.read(NOTICE_FILE_NAME))
    lucene_license = decodeUTF8(z.read(LICENSE_FILE_NAME))

    if LUCENE_LICENSE is None:
      raise RuntimeError("BUG in smokeTestRelease!")
    if LUCENE_NOTICE is None:
      raise RuntimeError("BUG in smokeTestRelease!")
    if notice != LUCENE_NOTICE:
      raise RuntimeError("%s: %s contents doesn't match main NOTICE.txt" % (desc, NOTICE_FILE_NAME))
    if lucene_license != LUCENE_LICENSE:
      raise RuntimeError("%s: %s contents doesn't match main LICENSE.txt" % (desc, LICENSE_FILE_NAME))


def normSlashes(path: str):
  return path.replace(os.sep, "/")


def checkAllJARs(topDir: str, gitRevision: str, version: str):
  print("    verify JAR metadata/identity/no javax.* or java.* classes...")
  for root, _, files in os.walk(topDir):
    normRoot = normSlashes(root)

    for file in files:
      if file.lower().endswith(".jar"):
        if normRoot.endswith("/replicator/lib") and file.startswith("javax.servlet"):
          continue
        fullPath = "%s/%s" % (root, file)
        noJavaPackageClasses('JAR file "%s"' % fullPath, fullPath)
        if file.lower().find("lucene") != -1:
          checkJARMetaData('JAR file "%s"' % fullPath, fullPath, gitRevision, version)


def checkSigs(urlString: str, version: str, tmpDir: str, isSigned: bool, keysFile: str):
  print("  test basics...")
  ents = getDirEntries(urlString)
  artifact = None
  changesURL = None
  mavenURL = None
  artifactURL = None
  expectedSigs: list[str] = []
  if isSigned:
    expectedSigs.append("asc")
  expectedSigs.extend(["sha512"])
  sigs: list[str] = []
  artifacts: list[tuple[str, str]] = []

  for text, subURL in ents:
    if text == "KEYS":
      raise RuntimeError("lucene: release dir should not contain a KEYS file - only toplevel /dist/lucene/KEYS is used")
    if text == "maven/":
      mavenURL = subURL
    elif text.startswith("changes"):
      if text not in ("changes/", "changes-%s/" % version):
        raise RuntimeError("lucene: found %s vs expected changes-%s/" % (text, version))
      changesURL = subURL
    elif artifact is None:
      artifact = text
      artifactURL = subURL
      expected = "lucene-%s" % version
      if not artifact.startswith(expected):
        raise RuntimeError("lucene: unknown artifact %s: expected prefix %s" % (text, expected))
      sigs = []
    elif text.startswith(artifact + "."):
      sigs.append(text[len(artifact) + 1 :])
    else:
      if sigs != expectedSigs:
        raise RuntimeError("lucene: artifact %s has wrong sigs: expected %s but got %s" % (artifact, expectedSigs, sigs))
      assert artifactURL is not None
      artifacts.append((artifact, artifactURL))
      artifact = text
      artifactURL = subURL
      sigs = []

  if sigs != []:
    assert artifact is not None
    assert artifactURL is not None
    artifacts.append((artifact, artifactURL))
    if sigs != expectedSigs:
      raise RuntimeError("lucene: artifact %s has wrong sigs: expected %s but got %s" % (artifact, expectedSigs, sigs))

  expected = ["lucene-%s-src.tgz" % version, "lucene-%s.tgz" % version]

  actual = [x[0] for x in artifacts]
  if expected != actual:
    raise RuntimeError("lucene: wrong artifacts: expected %s but got %s" % (expected, actual))

  # Set up clean gpg world; import keys file:
  gpgHomeDir = "%s/lucene.gpg" % tmpDir
  if os.path.exists(gpgHomeDir):
    shutil.rmtree(gpgHomeDir)
  Path(gpgHomeDir).mkdir(mode=0o700, parents=True)
  run("gpg --homedir %s --import %s" % (gpgHomeDir, keysFile), "%s/lucene.gpg.import.log" % tmpDir)

  if mavenURL is None:
    raise RuntimeError("lucene is missing maven")

  if changesURL is None:
    raise RuntimeError("lucene is missing changes-%s" % version)
  testChanges(version, changesURL)

  for artifact, urlString in artifacts:
    print("  download %s..." % artifact)
    scriptutil.download(artifact, urlString, tmpDir, force_clean=FORCE_CLEAN)
    verifyDigests(artifact, urlString, tmpDir)

    if isSigned:
      print("    verify sig")
      # Test sig (this is done with a clean brand-new GPG world)
      scriptutil.download(artifact + ".asc", urlString + ".asc", tmpDir, force_clean=FORCE_CLEAN)
      sigFile = "%s/%s.asc" % (tmpDir, artifact)
      artifactFile = "%s/%s" % (tmpDir, artifact)
      logFile = "%s/lucene.%s.gpg.verify.log" % (tmpDir, artifact)
      run("gpg --homedir %s --display-charset utf-8 --verify %s %s" % (gpgHomeDir, sigFile, artifactFile), logFile)
      # Forward any GPG warnings, except the expected one (since it's a clean world)
      with open(logFile) as f:
        print("File: %s" % logFile)
        for line in f.readlines():
          if line.lower().find("warning") != -1 and line.find("WARNING: This key is not certified with a trusted signature") == -1:
            print("      GPG: %s" % line.strip())

      # Test trust (this is done with the real users config)
      run("gpg --import %s" % (keysFile), "%s/lucene.gpg.trust.import.log" % tmpDir)
      print("    verify trust")
      logFile = "%s/lucene.%s.gpg.trust.log" % (tmpDir, artifact)
      run("gpg --display-charset utf-8 --verify %s %s" % (sigFile, artifactFile), logFile)
      # Forward any GPG warnings:
      with open(logFile) as f:
        for line in f.readlines():
          if line.lower().find("warning") != -1:
            print("      GPG: %s" % line.strip())


def testChanges(version: str, changesURLString: str):
  print("  check changes HTML...")
  changesURL = None
  for text, subURL in getDirEntries(changesURLString):
    if text == "Changes.html":
      changesURL = subURL

  if changesURL is None:
    raise RuntimeError("did not see Changes.html link from %s" % changesURLString)

  s = load(changesURL)
  checkChangesContent(s, version, changesURL, True)


def testChangesText(dir: str, version: str):
  "Checks all CHANGES.txt under this dir."
  for root, _, files in os.walk(dir):
    # NOTE: O(N) but N should be smallish:
    if "CHANGES.txt" in files:
      fullPath = "%s/CHANGES.txt" % root
      # print 'CHECK %s' % fullPath
      checkChangesContent(open(fullPath, encoding="UTF-8").read(), version, fullPath, False)


reChangesSectionHREF = re.compile('<a id="(.*?)".*?>(.*?)</a>', re.IGNORECASE)
reUnderbarNotDashHTML = re.compile(r"<li>(\s*(LUCENE)_\d\d\d\d+)")
reUnderbarNotDashTXT = re.compile(r"\s+((LUCENE)_\d\d\d\d+)", re.MULTILINE)


def checkChangesContent(s: str, version: str, name: str, isHTML: bool):
  currentVersionTuple = versionToTuple(version, name)

  if isHTML and s.find("Release %s" % version) == -1:
    raise RuntimeError('did not see "Release %s" in %s' % (version, name))

  if isHTML:
    r = reUnderbarNotDashHTML
  else:
    r = reUnderbarNotDashTXT

  m = r.search(s)
  if m is not None:
    raise RuntimeError("incorrect issue (_ instead of -) in %s: %s" % (name, m.group(1)))

  if s.lower().find("not yet released") != -1:
    raise RuntimeError('saw "not yet released" in %s' % name)

  if not isHTML:
    sub = "Lucene %s" % version
    if s.find(sub) == -1:
      # benchmark never seems to include release info:
      if name.find("/benchmark/") == -1:
        raise RuntimeError('did not see "%s" in %s' % (sub, name))

  if isHTML:
    # Make sure that a section only appears once under each release,
    # and that each release is not greater than the current version
    seenIDs: set[str] = set()
    seenText: set[str] = set()

    release = None
    for id, text in reChangesSectionHREF.findall(s):
      if text.lower().startswith("release "):
        release = text[8:].strip()
        seenText.clear()
        releaseTuple = versionToTuple(release, name)
        if releaseTuple > currentVersionTuple:
          raise RuntimeError("Future release %s is greater than %s in %s" % (release, version, name))
      if id in seenIDs:
        raise RuntimeError('%s has duplicate section "%s" under release "%s"' % (name, text, release))
      seenIDs.add(id)
      if text in seenText:
        raise RuntimeError('%s has duplicate section "%s" under release "%s"' % (name, text, release))
      seenText.add(text)


reVersion = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?\s*(-alpha|-beta|final|RC\d+)?\s*(?:\[.*\])?", re.IGNORECASE)


def versionToTuple(version: str, name: str):
  versionMatch = reVersion.match(version)
  if versionMatch is None:
    raise RuntimeError("Version %s in %s cannot be parsed" % (version, name))
  versionTuple = versionMatch.groups()
  while versionTuple[-1] is None or versionTuple[-1] == "":
    versionTuple = versionTuple[:-1]
  if versionTuple[-1].lower() == "-alpha":
    versionTuple = versionTuple[:-1] + ("0",)
  elif versionTuple[-1].lower() == "-beta":
    versionTuple = versionTuple[:-1] + ("1",)
  elif versionTuple[-1].lower() == "final":
    versionTuple = versionTuple[:-2] + ("100",)
  elif versionTuple[-1].lower()[:2] == "rc":
    versionTuple = versionTuple[:-2] + (versionTuple[-1][2:],)
  return tuple(int(x) if x is not None and x.isnumeric() else x for x in versionTuple)


reUnixPath = re.compile(r'\b[a-zA-Z_]+=(?:"(?:\\"|[^"])*"' + "|(?:\\\\.|[^\"'\\s])*" + r"|'(?:\\'|[^'])*')" + r'|(/(?:\\.|[^"\'\s])*)' + r'|("/(?:\\.|[^"])*")' + r"|('/(?:\\.|[^'])*')")


def unix2win(matchobj: re.Match[str]):
  if matchobj.group(1) is not None:
    return cygwinWindowsRoot + matchobj.group()
  if matchobj.group(2) is not None:
    return '"%s%s' % (cygwinWindowsRoot, matchobj.group().lstrip('"'))
  if matchobj.group(3) is not None:
    return "'%s%s" % (cygwinWindowsRoot, matchobj.group().lstrip("'"))
  return matchobj.group()


def cygwinifyPaths(command: str):
  # The problem: Native Windows applications running under Cygwin can't
  # handle Cygwin's Unix-style paths.  However, environment variable
  # values are automatically converted, so only paths outside of
  # environment variable values should be converted to Windows paths.
  # Assumption: all paths will be absolute.
  if "; gradlew " in command:
    command = reUnixPath.sub(unix2win, command)
  return command


def printFileContents(fileName: str):
  # Assume log file was written in system's default encoding, but
  # even if we are wrong, we replace errors ... the ASCII chars
  # (which is what we mostly care about eg for the test seed) should
  # still survive:
  txt = codecs.open(fileName, "r", encoding=sys.getdefaultencoding(), errors="replace").read()

  # Encode to our output encoding (likely also system's default
  # encoding):
  bytes = txt.encode(sys.stdout.encoding, errors="replace")

  # Decode back to string and print... we should hit no exception here
  # since all errors have been replaced:
  print(codecs.getdecoder(sys.stdout.encoding)(bytes)[0])
  print()


def run(command: str, logFile: str):
  if cygwin:
    command = cygwinifyPaths(command)
  if os.system("%s > %s 2>&1" % (command, logFile)):
    logPath: str = str(Path(logFile).resolve())
    print('\ncommand "%s" failed:' % command)
    printFileContents(logFile)
    raise RuntimeError('command "%s" failed; see log file %s' % (command, logPath))


def verifyDigests(artifact: str, urlString: str, tmpDir: str):
  print("    verify sha512 digest")
  sha512Expected, t = load(urlString + ".sha512").strip().split()
  if t != "*" + artifact:
    raise RuntimeError("SHA512 %s.sha512 lists artifact %s but expected *%s" % (urlString, t, artifact))

  s512 = hashlib.sha512()
  f = open("%s/%s" % (tmpDir, artifact), "rb")
  while True:
    x = f.read(65536)
    if len(x) == 0:
      break
    s512.update(x)
  f.close()
  sha512Actual = s512.hexdigest()
  if sha512Actual != sha512Expected:
    raise RuntimeError("SHA512 digest mismatch for %s: expected %s but got %s" % (artifact, sha512Expected, sha512Actual))


def getDirEntries(urlString: str):
  if urlString.startswith("file:/") and not urlString.startswith("file://"):
    # stupid bogus ant URI
    urlString = "file:///" + urlString[6:]

  if urlString.startswith("file://"):
    path = urlString[7:]
    path = path.removesuffix("/")
    if cygwin:  # Convert Windows path to Cygwin path
      path = re.sub(r"^/([A-Za-z]):/", r"/cygdrive/\1/", path)
    files: list[tuple[str, str]] = []
    for ent in os.listdir(path):
      entPath = "%s/%s" % (path, ent)
      if os.path.isdir(entPath):
        entPath += "/"
        ent += "/"
      files.append((ent, "file://%s" % entPath))
    files.sort()
    return files
  links = getHREFs(urlString)
  for i, (text, _) in enumerate(links):
    if text == "Parent Directory" or text == "..":
      return links[(i + 1) :]
  raise RuntimeError("could not enumerate %s" % (urlString))


def unpackAndVerify(java: Any, tmpDir: str, artifact: str, gitRevision: str, version: str, testArgs: str):
  destDir = "%s/unpack" % tmpDir
  if os.path.exists(destDir):
    shutil.rmtree(destDir)
  Path(destDir).mkdir(parents=True)
  os.chdir(destDir)
  print("  unpack %s..." % artifact)
  unpackLogFile = "%s/lucene-unpack-%s.log" % (tmpDir, artifact)
  if artifact.endswith(".tar.gz") or artifact.endswith(".tgz"):
    run("tar xzf %s/%s" % (tmpDir, artifact), unpackLogFile)
  elif artifact.endswith(".zip"):
    run("unzip %s/%s" % (tmpDir, artifact), unpackLogFile)

  # make sure it unpacks to proper subdir
  files = os.listdir(destDir)
  expected = "lucene-%s" % version
  if files != [expected]:
    raise RuntimeError("unpack produced entries %s; expected only %s" % (files, expected))

  unpackPath = "%s/%s" % (destDir, expected)
  verifyUnpacked(java, artifact, unpackPath, gitRevision, version, testArgs)
  return unpackPath


LUCENE_NOTICE = None
LUCENE_LICENSE = None


def is_in_list(in_folder: list[str], files: list[str], indent: int = 4):
  for fileName in files:
    print("%sChecking %s" % (" " * indent, fileName))
    found = False
    for f in [fileName, fileName + ".txt", fileName + ".md"]:
      if f in in_folder:
        in_folder.remove(f)
        found = True
    if not found:
      raise RuntimeError('file "%s" is missing' % fileName)


def verifyUnpacked(java: Any, artifact: str, unpackPath: str, gitRevision: str, version: str, testArgs: str):
  global LUCENE_NOTICE
  global LUCENE_LICENSE

  os.chdir(unpackPath)
  isSrc = artifact.find("-src") != -1

  # Check text files in release
  print("  %s" % artifact)
  in_root_folder = list(filter(lambda x: x[0] != ".", os.listdir(unpackPath)))
  in_lucene_folder: list[str] = []
  if isSrc:
    in_lucene_folder.extend(os.listdir(os.path.join(unpackPath, "lucene")))
    is_in_list(in_root_folder, ["LICENSE", "NOTICE", "README"])
    is_in_list(in_lucene_folder, ["JRE_VERSION_MIGRATION", "CHANGES", "MIGRATE", "SYSTEM_REQUIREMENTS"])
  else:
    is_in_list(in_root_folder, ["LICENSE", "NOTICE", "README", "JRE_VERSION_MIGRATION", "CHANGES", "MIGRATE", "SYSTEM_REQUIREMENTS"])

  if LUCENE_NOTICE is None:
    LUCENE_NOTICE = open("%s/NOTICE.txt" % unpackPath, encoding="UTF-8").read()
  if LUCENE_LICENSE is None:
    LUCENE_LICENSE = open("%s/LICENSE.txt" % unpackPath, encoding="UTF-8").read()

  # if not isSrc:
  #   # TODO: we should add verifyModule/verifySubmodule (e.g. analysis) here and recurse through
  #   expectedJARs = ()
  #
  #   for fileName in expectedJARs:
  #     fileName += '.jar'
  #     if fileName not in l:
  #       raise RuntimeError('lucene: file "%s" is missing from artifact %s' % (fileName, artifact))
  #     in_root_folder.remove(fileName)

  expected_folders = [
    "analysis",
    "analysis.tests",
    "backward-codecs",
    "benchmark",
    "benchmark-jmh",
    "classification",
    "codecs",
    "core",
    "core.tests",
    "distribution.tests",
    "demo",
    "expressions",
    "facet",
    "grouping",
    "highlighter",
    "join",
    "luke",
    "memory",
    "misc",
    "monitor",
    "queries",
    "queryparser",
    "replicator",
    "sandbox",
    "spatial-extras",
    "spatial-test-fixtures",
    "spatial3d",
    "suggest",
    "test-framework",
    "licenses",
  ]
  if isSrc:
    expected_src_root_files = [
      "build.gradle",
      "build-options.properties",
      "build-tools",
      "CONTRIBUTING.md",
      "dev-docs",
      "dev-tools",
      "gradle",
      "gradlew",
      "gradlew.bat",
      "help",
      "lucene",
      "settings.gradle",
      "versions.lock",
    ]
    expected_src_lucene_files = ["build.gradle", "documentation", "distribution", "dev-docs"]
    is_in_list(in_root_folder, expected_src_root_files)
    is_in_list(in_lucene_folder, expected_folders)
    is_in_list(in_lucene_folder, expected_src_lucene_files)
    if len(in_lucene_folder) > 0:
      raise RuntimeError("lucene: unexpected files/dirs in artifact %s lucene/ folder: %s" % (artifact, in_lucene_folder))
  else:
    is_in_list(in_root_folder, ["bin", "docs", "licenses", "modules", "modules-thirdparty", "modules-test-framework"])

  if len(in_root_folder) > 0:
    raise RuntimeError("lucene: unexpected files/dirs in artifact %s: %s" % (artifact, in_root_folder))

  if isSrc:
    print("    make sure no JARs/WARs in src dist...")
    lines = os.popen("find . -name \\*.jar -not -name \\*-api.jar").readlines()
    if len(lines) != 0:
      print("    FAILED:")
      for line in lines:
        print("      %s" % line.strip())
      raise RuntimeError("source release has JARs...")
    lines = os.popen("find . -name \\*.war").readlines()
    if len(lines) != 0:
      print("    FAILED:")
      for line in lines:
        print("      %s" % line.strip())
      raise RuntimeError("source release has WARs...")

    validateCmd = "./gradlew --no-daemon check -p lucene/documentation"
    print('    run "%s"' % validateCmd)
    java.run_java(validateCmd, "%s/validate.log" % unpackPath)

    print("    run tests w/ Java %s and testArgs='%s'..." % (BASE_JAVA_VERSION, testArgs))
    java.run_java("./gradlew --no-daemon test %s" % testArgs, "%s/test.log" % unpackPath)
    print("    compile jars w/ Java %s" % BASE_JAVA_VERSION)
    java.run_java("./gradlew --no-daemon jar -Dversion.release=%s" % version, "%s/compile.log" % unpackPath)
    testDemo(java.run_java, isSrc, version, BASE_JAVA_VERSION)

    if java.run_alt_javas:
      for run_alt_java, alt_java_version in zip(java.run_alt_javas, java.alt_java_versions, strict=False):
        print("    run tests w/ Java %s and testArgs='%s'..." % (alt_java_version, testArgs))
        run_alt_java("./gradlew --no-daemon test %s" % testArgs, "%s/test.log" % unpackPath)
        print("    compile jars w/ Java %s" % alt_java_version)
        run_alt_java("./gradlew --no-daemon jar -Dversion.release=%s" % version, "%s/compile.log" % unpackPath)
        testDemo(run_alt_java, isSrc, version, alt_java_version)

    print("  confirm all releases have coverage in TestBackwardsCompatibility")
    confirmAllReleasesAreTestedForBackCompat(version, unpackPath)

  else:
    checkAllJARs(os.getcwd(), gitRevision, version)

    testDemo(java.run_java, isSrc, version, BASE_JAVA_VERSION)
    if java.run_alt_javas:
      for run_alt_java, alt_java_version in zip(java.run_alt_javas, java.alt_java_versions, strict=False):
        testDemo(run_alt_java, isSrc, version, alt_java_version)

  testChangesText(".", version)


def testDemo(run_java: Callable[[str, str], None], isSrc: bool, version: str, jdk: str):
  if os.path.exists("index"):
    shutil.rmtree("index")  # nuke any index from any previous iteration

  print("    test demo with %s..." % jdk)
  sep = ";" if cygwin else ":"
  if isSrc:
    # For source release, use the classpath for each module.
    classPath = [
      "lucene/core/build/libs/lucene-core-%s.jar" % version,
      "lucene/demo/build/libs/lucene-demo-%s.jar" % version,
      "lucene/analysis/common/build/libs/lucene-analyzers-common-%s.jar" % version,
      "lucene/queryparser/build/libs/lucene-queryparser-%s.jar" % version,
    ]
    cp = sep.join(classPath)
    docsDir = "lucene/core/src"
    checkIndexCmd = 'java -ea -cp "%s" org.apache.lucene.index.CheckIndex index' % cp
    indexFilesCmd = 'java -cp "%s" -Dsmoketester=true org.apache.lucene.demo.IndexFiles -index index -docs %s' % (cp, docsDir)
    searchFilesCmd = 'java -cp "%s" org.apache.lucene.demo.SearchFiles -index index -query lucene' % cp
  else:
    # For binary release, set up module path.
    cp = "--module-path %s" % (sep.join(["modules", "modules-thirdparty"]))
    docsDir = "docs"
    checkIndexCmd = "java -ea %s --module org.apache.lucene.core/org.apache.lucene.index.CheckIndex index" % cp
    indexFilesCmd = "java -Dsmoketester=true %s --module org.apache.lucene.demo/org.apache.lucene.demo.IndexFiles -index index -docs %s" % (cp, docsDir)
    searchFilesCmd = "java %s --module org.apache.lucene.demo/org.apache.lucene.demo.SearchFiles -index index -query lucene" % cp

  run_java(indexFilesCmd, "index.log")
  run_java(searchFilesCmd, "search.log")
  reMatchingDocs = re.compile(r"(\d+) total matching documents")
  m = reMatchingDocs.search(open("search.log", encoding="UTF-8").read())
  if m is None:
    raise RuntimeError("lucene demo's SearchFiles found no results")
  numHits = int(m.group(1))
  if numHits < 100:
    raise RuntimeError("lucene demo's SearchFiles found too few results: %s" % numHits)
  print('      got %d hits for query "lucene"' % numHits)

  print("    checkindex with %s..." % jdk)
  run_java(checkIndexCmd, "checkindex.log")
  s = open("checkindex.log").read()
  m = re.search(r"^\s+version=(.*?)$", s, re.MULTILINE)
  if m is None:
    raise RuntimeError("unable to locate version=NNN output from CheckIndex; see checkindex.log")
  actualVersion = m.group(1)
  if removeTrailingZeros(actualVersion) != removeTrailingZeros(version):
    raise RuntimeError('wrong version from CheckIndex: got "%s" but expected "%s"' % (actualVersion, version))


def removeTrailingZeros(version: str):
  return re.sub(r"(\.0)*$", "", version)


def checkMaven(baseURL: str, tmpDir: str, gitRevision: str, version: str, isSigned: bool, keysFile: str):
  print("    download artifacts")
  artifacts: list[str] = []
  artifactsURL = "%s/lucene/maven/org/apache/lucene/" % baseURL
  targetDir = "%s/maven/org/apache/lucene" % tmpDir
  if not os.path.exists(targetDir):
    Path(targetDir).mkdir(parents=True)
  crawl(artifacts, artifactsURL, targetDir)
  print()
  verifyPOMperBinaryArtifact(artifacts, version)
  verifyMavenDigests(artifacts)
  checkJavadocAndSourceArtifacts(artifacts, version)
  verifyDeployedPOMsCoordinates(artifacts, version)
  if isSigned:
    verifyMavenSigs(tmpDir, artifacts, keysFile)

  distFiles = getBinaryDistFiles(tmpDir, version, baseURL)
  checkIdenticalMavenArtifacts(distFiles, artifacts, version)

  checkAllJARs("%s/maven/org/apache/lucene" % tmpDir, gitRevision, version)


def getBinaryDistFiles(tmpDir: str, version: str, baseURL: str):
  distribution = "lucene-%s.tgz" % version
  if not os.path.exists("%s/%s" % (tmpDir, distribution)):
    distURL = "%s/lucene/%s" % (baseURL, distribution)
    print("    download %s..." % distribution, end=" ")
    scriptutil.download(distribution, distURL, tmpDir, force_clean=FORCE_CLEAN)
  destDir = "%s/unpack-lucene-getBinaryDistFiles" % tmpDir
  if os.path.exists(destDir):
    shutil.rmtree(destDir)
  Path(destDir).mkdir(parents=True)
  os.chdir(destDir)
  print("    unpack %s..." % distribution)
  unpackLogFile = "%s/unpack-%s-getBinaryDistFiles.log" % (tmpDir, distribution)
  run("tar xzf %s/%s" % (tmpDir, distribution), unpackLogFile)
  distributionFiles: list[str] = []
  for root, _, files in os.walk(destDir):
    distributionFiles.extend([os.path.join(root, file) for file in files])
  return distributionFiles


def checkJavadocAndSourceArtifacts(artifacts: list[str], version: str):
  print("    check for javadoc and sources artifacts...")
  for artifact in artifacts:
    if artifact.endswith(version + ".jar"):
      javadocJar = artifact[:-4] + "-javadoc.jar"
      if javadocJar not in artifacts:
        raise RuntimeError("missing: %s" % javadocJar)
      sourcesJar = artifact[:-4] + "-sources.jar"
      if sourcesJar not in artifacts:
        raise RuntimeError("missing: %s" % sourcesJar)


def getZipFileEntries(fileName: str):
  entries: list[str] = []
  with zipfile.ZipFile(fileName) as zf:
    for zi in zf.infolist():
      entries.append(zi.filename)
  # Sort by name:
  entries.sort()
  return entries


def checkIdenticalMavenArtifacts(distFiles: list[str], artifacts: list[str], version: str):
  print("    verify that Maven artifacts are same as in the binary distribution...")
  reJarWar = re.compile(r"%s\.[wj]ar$" % version)  # exclude *-javadoc.jar and *-sources.jar
  distFilenames: dict[str, str] = dict()
  for file in distFiles:
    baseName = os.path.basename(file)
    distFilenames[baseName] = file
  for artifact in artifacts:
    if reJarWar.search(artifact):
      artifactFilename = os.path.basename(artifact)
      if artifactFilename not in distFilenames:
        raise RuntimeError("Maven artifact %s is not present in lucene binary distribution" % artifact)
      identical = filecmp.cmp(artifact, distFilenames[artifactFilename], shallow=False)
      if not identical:
        raise RuntimeError("Maven artifact %s is not identical to %s in lucene binary distribution" % (artifact, distFilenames[artifactFilename]))


def verifyMavenDigests(artifacts: list[str]):
  print("    verify Maven artifacts' md5/sha1 digests...")
  reJarWarPom = re.compile(r"\.(?:[wj]ar|pom)$")
  for artifactFile in [a for a in artifacts if reJarWarPom.search(a)]:
    if artifactFile + ".md5" not in artifacts:
      raise RuntimeError("missing: MD5 digest for %s" % artifactFile)
    if artifactFile + ".sha1" not in artifacts:
      raise RuntimeError("missing: SHA1 digest for %s" % artifactFile)
    with open(artifactFile + ".md5", encoding="UTF-8") as md5File:
      md5Expected = md5File.read().strip()
    with open(artifactFile + ".sha1", encoding="UTF-8") as sha1File:
      sha1Expected = sha1File.read().strip()
    md5 = hashlib.md5()
    sha1 = hashlib.sha1()
    inputFile = open(artifactFile, "rb")
    while True:
      bytes = inputFile.read(65536)
      if len(bytes) == 0:
        break
      md5.update(bytes)
      sha1.update(bytes)
    inputFile.close()
    md5Actual = md5.hexdigest()
    sha1Actual = sha1.hexdigest()
    if md5Actual != md5Expected:
      raise RuntimeError("MD5 digest mismatch for %s: expected %s but got %s" % (artifactFile, md5Expected, md5Actual))
    if sha1Actual != sha1Expected:
      raise RuntimeError("SHA1 digest mismatch for %s: expected %s but got %s" % (artifactFile, sha1Expected, sha1Actual))


def getPOMcoordinate(treeRoot: ET.Element):
  namespace = "{http://maven.apache.org/POM/4.0.0}"
  groupId = treeRoot.find("%sgroupId" % namespace)
  if groupId is None:
    groupId = treeRoot.find(f"{namespace}parent/{namespace}groupId")
  assert groupId is not None
  assert groupId.text is not None
  groupId = groupId.text.strip()

  artifactId = treeRoot.find("%sartifactId" % namespace)
  assert artifactId is not None
  assert artifactId.text is not None
  artifactId = artifactId.text.strip()

  version = treeRoot.find("%sversion" % namespace)
  if version is None:
    version = treeRoot.find(f"{namespace}parent/{namespace}version")
  assert version is not None
  assert version.text is not None
  version = version.text.strip()

  packaging = treeRoot.find("%spackaging" % namespace)
  if packaging is None:
    packaging = "jar"
  else:
    assert packaging.text is not None
    packaging = packaging.text.strip()
  return groupId, artifactId, packaging, version


def verifyMavenSigs(tmpDir: str, artifacts: list[str], keysFile: str):
  print("    verify maven artifact sigs", end=" ")

  # Set up clean gpg world; import keys file:
  gpgHomeDir = "%s/lucene.gpg" % tmpDir
  if os.path.exists(gpgHomeDir):
    shutil.rmtree(gpgHomeDir)
  Path(gpgHomeDir).mkdir(mode=0o700, parents=True)
  run("gpg --homedir %s --import %s" % (gpgHomeDir, keysFile), "%s/lucene.gpg.import.log" % tmpDir)

  reArtifacts = re.compile(r"\.(?:pom|[jw]ar)$")
  for artifactFile in [a for a in artifacts if reArtifacts.search(a)]:
    artifact = os.path.basename(artifactFile)
    sigFile = "%s.asc" % artifactFile
    # Test sig (this is done with a clean brand-new GPG world)
    logFile = "%s/lucene.%s.gpg.verify.log" % (tmpDir, artifact)
    run("gpg --display-charset utf-8 --homedir %s --verify %s %s" % (gpgHomeDir, sigFile, artifactFile), logFile)

    # Forward any GPG warnings, except the expected one (since it's a clean world)
    print_warnings_in_file(logFile)

    # Test trust (this is done with the real users config)
    run("gpg --import %s" % keysFile, "%s/lucene.gpg.trust.import.log" % tmpDir)
    logFile = "%s/lucene.%s.gpg.trust.log" % (tmpDir, artifact)
    run("gpg --display-charset utf-8 --verify %s %s" % (sigFile, artifactFile), logFile)
    # Forward any GPG warnings:
    print_warnings_in_file(logFile)

    sys.stdout.write(".")
  print()


def print_warnings_in_file(file: str):
  with open(file) as f:
    for line in f.readlines():
      if line.lower().find("warning") != -1 and line.find("WARNING: This key is not certified with a trusted signature") == -1 and line.find("WARNING: using insecure memory") == -1:
        print("      GPG: %s" % line.strip())


def verifyPOMperBinaryArtifact(artifacts: list[str], version: str):
  print("    verify that each binary artifact has a deployed POM...")
  reBinaryJarWar = re.compile(r"%s\.[jw]ar$" % re.escape(version))
  for artifact in [a for a in artifacts if reBinaryJarWar.search(a)]:
    POM = artifact[:-4] + ".pom"
    if POM not in artifacts:
      raise RuntimeError("missing: POM for %s" % artifact)


def verifyDeployedPOMsCoordinates(artifacts: list[str], version: str):
  """Verify that each POM's coordinate (drawn from its content) matches
  its filepath, and verify that the corresponding artifact exists.
  """
  print("    verify deployed POMs' coordinates...")
  for POM in [a for a in artifacts if a.endswith(".pom")]:
    treeRoot = ET.parse(POM).getroot()
    groupId, artifactId, packaging, POMversion = getPOMcoordinate(treeRoot)
    POMpath = "%s/%s/%s/%s-%s.pom" % (groupId.replace(".", "/"), artifactId, version, artifactId, version)
    if not POM.endswith(POMpath):
      raise RuntimeError("Mismatch between POM coordinate %s:%s:%s and filepath: %s" % (groupId, artifactId, POMversion, POM))
    # Verify that the corresponding artifact exists
    artifact = POM[:-3] + packaging
    if artifact not in artifacts:
      raise RuntimeError("Missing corresponding .%s artifact for POM %s" % (packaging, POM))


def crawl(downloadedFiles: list[str], urlString: str, targetDir: str, exclusions: set[str] | None = None):
  exclude: set[str] = exclusions if exclusions else set()
  for text, subURL in getDirEntries(urlString):
    if text not in exclude:
      path = os.path.join(targetDir, text)
      if text.endswith("/"):
        if not os.path.exists(path):
          Path(path).mkdir(parents=True)
        crawl(downloadedFiles, subURL, path, exclusions)
      else:
        if not os.path.exists(path) or FORCE_CLEAN:
          scriptutil.download(text, subURL, targetDir, quiet=True, force_clean=FORCE_CLEAN)
        downloadedFiles.append(path)
        sys.stdout.write(".")


def make_java_config(parser: argparse.ArgumentParser, alt_java_homes: list[str]):
  def _make_runner(java_home: str, is_base_version: bool = False):
    if cygwin:
      java_home = subprocess.check_output('cygpath -u "%s"' % java_home, shell=True).decode("utf-8").strip()
    cmd_prefix = 'export JAVA_HOME="%s" PATH="%s/bin:$PATH" JAVACMD="%s/bin/java"' % (java_home, java_home, java_home)
    s = subprocess.check_output("%s; java -version" % cmd_prefix, shell=True, stderr=subprocess.STDOUT).decode("utf-8")

    match = re.search(r'version "([1-9][0-9]*)', s)
    assert match
    actual_version = match.group(1)
    print("Java %s JAVA_HOME=%s" % (actual_version, java_home))

    # validate Java version
    if is_base_version:
      if actual_version != BASE_JAVA_VERSION:
        parser.error("got wrong base version for java %s:\n%s" % (BASE_JAVA_VERSION, s))
    elif int(actual_version) < int(BASE_JAVA_VERSION):
      parser.error("got wrong version for java %s, less than base version %s:\n%s" % (actual_version, BASE_JAVA_VERSION, s))

    def run_java(cmd: str, logfile: str):
      run("%s; %s" % (cmd_prefix, cmd), logfile)

    return run_java, actual_version

  java_home = os.environ.get("JAVA_HOME")
  if java_home is None:
    parser.error("JAVA_HOME must be set")
  run_java, _ = _make_runner(java_home, True)
  run_alt_javas: list[Callable[[str, str], None]] = []
  alt_java_versions: list[str] = []
  if alt_java_homes:
    for alt_java_home in alt_java_homes:
      run_alt_java, version = _make_runner(alt_java_home)
      run_alt_javas.append(run_alt_java)
      alt_java_versions.append(version)

  jc = namedtuple("JavaConfig", "run_java java_home run_alt_javas alt_java_homes alt_java_versions")
  return jc(run_java, java_home, run_alt_javas, alt_java_homes, alt_java_versions)


version_re = re.compile(r"(\d+\.\d+\.\d+(-ALPHA|-BETA)?)")
revision_re = re.compile(r"rev-([a-f\d]+)")


def parse_config():
  epilogue = textwrap.dedent("""
    Example usage:
    python3 -u dev-tools/scripts/smokeTestRelease.py https://dist.apache.org/repos/dist/dev/lucene/lucene-9.0.0-RC1-rev-c7510a0...
  """)
  description = "Utility to test a release."
  parser = argparse.ArgumentParser(description=description, epilog=epilogue, formatter_class=argparse.RawDescriptionHelpFormatter)
  parser.add_argument("--tmp-dir", metavar="PATH", help="Temporary directory to test inside, defaults to /tmp/smoke_lucene_$version_$revision")
  parser.add_argument("--not-signed", dest="is_signed", action="store_false", default=True, help="Indicates the release is not signed")
  parser.add_argument("--local-keys", metavar="PATH", help="Uses local KEYS file instead of fetching from https://archive.apache.org/dist/lucene/KEYS")
  parser.add_argument("--revision", help="GIT revision number that release was built with, defaults to that in URL")
  parser.add_argument("--version", metavar="X.Y.Z(-ALPHA|-BETA)?", help="Version of the release, defaults to that in URL")
  parser.add_argument("--test-alternative-java", action="append", help="Path to alternative Java home directory, to run tests with if specified")
  parser.add_argument("--download-only", action="store_true", default=False, help="Only perform download and sha hash check steps")
  parser.add_argument("url", help="Url pointing to release to test")
  parser.add_argument("test_args", nargs=argparse.REMAINDER, help="Arguments to pass to gradle for testing, e.g. -Dwhat=ever.")
  c = parser.parse_args()

  if c.version is not None:
    if not version_re.match(c.version):
      parser.error('version "%s" does not match format X.Y.Z[-ALPHA|-BETA]' % c.version)
  else:
    version_match = version_re.search(c.url)
    if version_match is None:
      parser.error("Could not find version in URL")
    c.version = version_match.group(1)

  if c.revision is None:
    revision_match = revision_re.search(c.url)
    if revision_match is None:
      parser.error("Could not find revision in URL")
    c.revision = revision_match.group(1)
    print("Revision: %s" % c.revision)

  if c.local_keys is not None and not os.path.exists(c.local_keys):
    parser.error('Local KEYS file "%s" not found' % c.local_keys)

  c.java = make_java_config(parser, c.test_alternative_java)

  if c.tmp_dir:
    c.tmp_dir = str(Path(c.tmp_dir).resolve())
  else:
    tmp = "/tmp/smoke_lucene_%s_%s" % (c.version, c.revision)
    c.tmp_dir = tmp
    i = 1
    while os.path.exists(c.tmp_dir):
      c.tmp_dir = tmp + "_%d" % i
      i += 1

  return c


reVersion1 = re.compile(r"\>(\d+)\.(\d+)\.(\d+)(-alpha|-beta)?/\<", re.IGNORECASE)
reVersion2 = re.compile(r"-(\d+)\.(\d+)\.(\d+)(-alpha|-beta)?\.", re.IGNORECASE)


def getAllLuceneReleases():
  s = load("https://archive.apache.org/dist/lucene/java")

  releases: set[tuple[int, ...]] = set()
  for r in reVersion1, reVersion2:
    for tup in r.findall(s):
      if tup[-1].lower() == "-alpha":
        tup = tup[:3] + ("0",)
      elif tup[-1].lower() == "-beta":
        tup = tup[:3] + ("1",)
      elif tup[-1] == "":
        tup = tup[:3]
      else:
        raise RuntimeError("failed to parse version: %s" % tup[-1])
      releases.add(tuple(int(x) for x in tup))

  releaseList = list(releases)
  releaseList.sort()
  return releaseList


def confirmAllReleasesAreTestedForBackCompat(smokeVersion: str, unpackPath: str):
  print("    find all past Lucene releases...")
  allReleases = getAllLuceneReleases()
  # for tup in allReleases:
  #  print('  %s' % '.'.join(str(x) for x in tup))

  testedIndicesPaths = glob.glob("%s/lucene/backward-codecs/src/test/org/apache/lucene/backward_index/*-cfs.zip" % unpackPath)
  testedIndices: set[tuple[int, ...]] = set()

  reIndexName = re.compile(r"^[^.]*.(.*?)-cfs.zip")
  for name in testedIndicesPaths:
    basename = os.path.basename(name)
    match = reIndexName.fullmatch(basename)
    assert match
    version = match.group(1)
    tup = tuple(version.split("."))
    if len(tup) == 3:
      # ok
      tup = tuple(int(x) for x in tup)
    elif tup == ("4", "0", "0", "1"):
      # CONFUSING: this is the 4.0.0-alpha index??
      tup = 4, 0, 0, 0
    elif tup == ("4", "0", "0", "2"):
      # CONFUSING: this is the 4.0.0-beta index??
      tup = 4, 0, 0, 1
    elif basename == "unsupported.5x-with-4x-segments-cfs.zip":
      # Mixed version test case; ignore it for our purposes because we only
      # tally up the "tests single Lucene version" indices
      continue
    elif basename == "unsupported.5.0.0.singlesegment-cfs.zip":
      tup = 5, 0, 0
    else:
      raise RuntimeError("could not parse version %s" % name)

    testedIndices.add(tup)

  if False:
    indexList = list(testedIndices)
    indexList.sort()
    for release in indexList:
      print("  %s" % ".".join(str(x) for x in release))

  allReleases = set(allReleases)

  for x in testedIndices:
    if x not in allReleases:
      # Curious: we test 1.9.0 index but it's not in the releases (I think it was pulled because of nasty bug?)
      if x != (1, 9, 0):
        raise RuntimeError("tested version=%s but it was not released?" % ".".join(str(y) for y in x))

  notTested: list[tuple[int, ...]] = []
  for x in allReleases:
    if x not in testedIndices:
      releaseVersion = ".".join(str(y) for y in x)
      if releaseVersion in ("1.4.3", "1.9.1", "2.3.1", "2.3.2"):
        # Exempt the dark ages indices
        continue
      if x >= tuple(int(y) for y in smokeVersion.split(".")):
        # Exempt versions not less than the one being smoke tested
        print("      Backcompat testing not required for release %s because it's not less than %s" % (releaseVersion, smokeVersion))
        continue
      notTested.append(x)

  if len(notTested) > 0:
    notTested.sort()
    print("Releases that don't seem to be tested:")
    for x in notTested:
      print("  %s" % ".".join(str(y) for y in x))
    raise RuntimeError("some releases are not tested by TestBackwardsCompatibility?")
  print("    success!")


def main():
  c = parse_config()

  # Pick <major>.<minor> part of version and require script to be from same branch
  match = re.search(r"((\d+).(\d+)).(\d+)", scriptutil.find_current_version())
  assert match
  scriptVersion = match.group(1).strip()
  if not c.version.startswith(scriptVersion + "."):
    raise RuntimeError("smokeTestRelease.py for %s.X is incompatible with a %s release." % (scriptVersion, c.version))

  print("NOTE: output encoding is %s" % sys.stdout.encoding)
  smokeTest(c.java, c.url, c.revision, c.version, c.tmp_dir, c.is_signed, c.local_keys, " ".join(c.test_args), downloadOnly=c.download_only)


def smokeTest(java: Any, baseURL: str, gitRevision: str, version: str, tmpDir: str, isSigned: bool, local_keys: str | None, testArgs: str, downloadOnly: bool = False):
  startTime = datetime.datetime.now()

  # Tests annotated @Nightly are more resource-intensive but often cover
  # important code paths. They're disabled by default to preserve a good
  # developer experience, but we enable them for smoke tests where we want good
  # coverage.
  testArgs = "-Dtests.nightly=true %s" % testArgs

  # We also enable GUI tests in smoke tests (LUCENE-10531)
  testArgs = "-Dtests.gui=true %s" % testArgs

  if FORCE_CLEAN:
    if os.path.exists(tmpDir):
      raise RuntimeError("temp dir %s exists; please remove first" % tmpDir)

  if not os.path.exists(tmpDir):
    Path(tmpDir).mkdir(parents=True)

  lucenePath = None
  print()
  print('Load release URL "%s"...' % baseURL)
  newBaseURL = unshortenURL(baseURL)
  if newBaseURL != baseURL:
    print("  unshortened: %s" % newBaseURL)
    baseURL = newBaseURL

  for text, subURL in getDirEntries(baseURL):
    if text.lower().find("lucene") != -1:
      lucenePath = subURL

  if lucenePath is None:
    raise RuntimeError("could not find lucene subdir")

  print()
  print("Get KEYS...")
  if local_keys is not None:
    print("    Using local KEYS file %s" % local_keys)
    keysFile = local_keys
  else:
    keysFileURL = "https://archive.apache.org/dist/lucene/KEYS"
    print("    Downloading online KEYS file %s" % keysFileURL)
    scriptutil.download("KEYS", keysFileURL, tmpDir, force_clean=FORCE_CLEAN)
    keysFile = "%s/KEYS" % (tmpDir)

  print()
  print("Test Lucene...")
  checkSigs(lucenePath, version, tmpDir, isSigned, keysFile)
  if not downloadOnly:
    unpackAndVerify(java, tmpDir, "lucene-%s.tgz" % version, gitRevision, version, testArgs)
    unpackAndVerify(java, tmpDir, "lucene-%s-src.tgz" % version, gitRevision, version, testArgs)
    print()
    print("Test Maven artifacts...")
    checkMaven(baseURL, tmpDir, gitRevision, version, isSigned, keysFile)
  else:
    print("\nLucene test done (--download-only specified)")

  print("\nSUCCESS! [%s]\n" % (datetime.datetime.now() - startTime))


if __name__ == "__main__":
  try:
    main()
  except KeyboardInterrupt:
    print("Keyboard interrupt...exiting")
