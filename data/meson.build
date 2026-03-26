install_data('io.github.cellar.desktop',
  install_dir: get_option('datadir') / 'applications',
)

gnome.compile_schemas(depend_files: files('io.github.cellar.gschema.xml'))
install_data('io.github.cellar.gschema.xml',
  install_dir: get_option('datadir') / 'glib-2.0' / 'schemas',
)

install_subdir('icons/hicolor',
  install_dir: get_option('datadir') / 'icons',
)

install_data('dosbox-staging.conf',
  install_dir: get_option('datadir') / 'cellar',
)

install_data('dosbox-profiles.json',
  install_dir: get_option('datadir') / 'cellar',
)

subdir('ui')
