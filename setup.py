import setuptools

setuptools.setup(
    name="nixprof",
    packages=["."],
    entry_points='''
        [console_scripts]
        nixprof=nixprof:main
    '''
)
