# Keep multiprocessing dispatch before imports that load Qt or numerical libraries.
from multiprocessing import freeze_support

if __name__ == '__main__':
    freeze_support()
    from flowhero.cli import main
    main()
