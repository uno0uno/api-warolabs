-- Add missing Colombian municipalities (DANE DIVIPOLA vs public_cities diff)
-- Issue warocol.com#1740
INSERT INTO public_cities (
  country, city, city_slug, is_active, sort_order,
  department_code, department_name, municipality_code, municipality_type, latitude, longitude
) VALUES
  ('Colombia', 'San Andrés', 'san-andres-isla', true, 9000, '88', 'Archipiélago De San Andrés, Providencia Y Santa Catalina', '88001', 'Isla', '12.578108', '-81.707181'),
  ('Colombia', 'El Encanto', 'el-encanto', true, 9000, '91', 'Amazonas', '91263', 'Área no municipalizada', '-1.74806', '-73.207114'),
  ('Colombia', 'La Chorrera', 'la-chorrera', true, 9000, '91', 'Amazonas', '91405', 'Área no municipalizada', '-1.442617', '-72.791889'),
  ('Colombia', 'La Pedrera', 'la-pedrera', true, 9000, '91', 'Amazonas', '91407', 'Área no municipalizada', '-1.320301', '-69.585499'),
  ('Colombia', 'La Victoria', 'la-victoria', true, 9000, '91', 'Amazonas', '91430', 'Área no municipalizada', '0.054936', '-71.223208'),
  ('Colombia', 'Mirití - Paraná', 'miriti-parana', true, 9000, '91', 'Amazonas', '91460', 'Área no municipalizada', '-0.888833', '-70.98893'),
  ('Colombia', 'Puerto Alegría', 'puerto-alegria', true, 9000, '91', 'Amazonas', '91530', 'Área no municipalizada', '-1.005674', '-74.014461'),
  ('Colombia', 'Puerto Arica', 'puerto-arica', true, 9000, '91', 'Amazonas', '91536', 'Área no municipalizada', '-2.147039', '-71.752186'),
  ('Colombia', 'Puerto Santander', 'puerto-santander-amazonas', true, 9000, '91', 'Amazonas', '91669', 'Área no municipalizada', '-0.621184', '-72.384213'),
  ('Colombia', 'Tarapacá', 'tarapaca', true, 9000, '91', 'Amazonas', '91798', 'Área no municipalizada', '-2.890126', '-69.741745'),
  ('Colombia', 'San Felipe', 'san-felipe', true, 9000, '94', 'Guainía', '94883', 'Área no municipalizada', '1.912495', '-67.067848'),
  ('Colombia', 'Puerto Colombia', 'puerto-colombia-guainia', true, 9000, '94', 'Guainía', '94884', 'Área no municipalizada', '2.726438', '-67.566774'),
  ('Colombia', 'La Guadalupe', 'la-guadalupe', true, 9000, '94', 'Guainía', '94885', 'Área no municipalizada', '1.632464', '-66.963692'),
  ('Colombia', 'Cacahual', 'cacahual', true, 9000, '94', 'Guainía', '94886', 'Área no municipalizada', '3.52617', '-67.413312'),
  ('Colombia', 'Pana Pana', 'pana-pana', true, 9000, '94', 'Guainía', '94887', 'Área no municipalizada', '1.865668', '-69.0099'),
  ('Colombia', 'Morichal', 'morichal', true, 9000, '94', 'Guainía', '94888', 'Área no municipalizada', '2.265132', '-69.919404'),
  ('Colombia', 'Pacoa', 'pacoa', true, 9000, '97', 'Vaupés', '97511', 'Área no municipalizada', '0.020698', '-71.004339'),
  ('Colombia', 'Papunahua', 'papunahua', true, 9000, '97', 'Vaupés', '97777', 'Área no municipalizada', '1.908124', '-70.76091'),
  ('Colombia', 'Yavaraté', 'yavarate', true, 9000, '97', 'Vaupés', '97889', 'Área no municipalizada', '0.609142', '-69.203337')
ON CONFLICT (city_slug) DO NOTHING;
