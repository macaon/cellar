project('cellar',
  version: '0.1.0',
  meson_version: '>= 1.0.0',
  license: 'GPL-3.0-or-later',
)

python = import('python').find_installation('python3', required: true)
gnome = import('gnome')
i18n = import('i18n')

app_id = 'io.github.cellar'

subdir('cellar')
subdir('data')
subdir('po')

gnome.post_install(
  glib_compile_schemas: true,
  gtk_update_icon_cache: true,
  update_desktop_database: true,
)
