python_sources = files(
  '__init__.py',
  'base_store.py',
  'config.py',
  'database.py',
  'detect.py',
  'dosbox.py',
  'install_queue.py',
  'installer.py',
  'manifest.py',
  'packager.py',
  'project.py',
  'repo.py',
  'runners.py',
  'sandbox.py',
  'steam.py',
  'umu.py',
  'updater.py',
)

python.install_sources(python_sources, subdir: 'cellar/backend')

install_data('launch_exclude.txt', install_dir: python.get_install_dir() / 'cellar/backend')
install_data('backup_exclude.txt', install_dir: python.get_install_dir() / 'cellar/backend')
