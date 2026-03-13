project('cellar',
  version: '0.61.0',
  meson_version: '>= 1.0.0',
  license: 'GPL-3.0-or-later',
)

python = import('python').find_installation('python3', required: true)
gnome = import('gnome')

app_id = 'io.github.cellar'

install_data(
  'bin/cellar',
  install_dir: get_option('bindir'),
  install_mode: 'rwxr-xr-x',
)

subdir('cellar')
subdir('data')

gnome.post_install(
  gtk_update_icon_cache: true,
  update_desktop_database: true,
)
