#!/usr/bin/env python3

from pathlib import Path
import sys

from simplui import defaultFlow, ShellTask

def findFileByExt(indir, testext):
    outlist = []
    for cpath in indir.iterdir():
        if cpath.is_file() and cpath.suffix == testext:
            outlist.append(cpath)
        elif cpath.is_dir():
            outlist = outlist + findsourcefiles(cpath, testext)
    return outlist

class CCompileObject(ShellTask):
    def __init__(self):
        super().__init__()
        ## adds command, precommands, postcommands
        ## input, output, name
        self.compiler = 'g++'
        self.includes = []
        self.options = '-Wall'
    def addinclude(self, incpath):
        self.includes.append(incpath)
    def setup(self):
        outcmdlist = [self.compiler, self.options, '-c', self.input,
                      '-o', self.output]
        for cinclude in self.includes:
            outcmdlist.append(f"-I{cinclude}")
        self.command = ' '.join(outcmdlist)
        self.name = f"compile_obj_{self.input.stem}"

class CCLink(CCompileObject):
    def __init__(self):
        super().__init__()
        self.inputs = []
    def setup(self):
        outcmdlist = [self.compiler, self.options, '-o', self.output]
        outcmdlist += self.inputs
        self.command = ' '.join(outcmdlist)
        self.name = f"link_obj_{self.output.stem}"

        
commonIncludes = "./includes"

sourcedir = Path(".").resolve()
cppfiles = findFileByExt(sourcedir, '.cpp')
objdir = sourcedir / 'object_files'

for ccpath in cppfiles:
    ctask = CCompileObject()
    ctask.addInclude(commonIncludes)
    ctask.input = ccpath
    ctask.output = objdir / ccpath.stem + ".o"
    ctask.setup()
    defaultFlow.addTask(ctask)

outputfname = 'app'
ctask = CCLink()
ctask.output = outputfname
ctask.setup()
defaultFlow.addTask(ctask)



