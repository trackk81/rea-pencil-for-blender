"""Blender API names shared by the supported preview versions."""
import bpy
import numpy as np


def grease_pencil_data():
    collection = getattr(bpy.data, 'grease_pencils_v3', None)
    return collection if collection is not None else bpy.data.grease_pencils

def lineart_width_property(modifier):
    return 'thickness' if hasattr(modifier, 'thickness') else 'radius'


def lineart_width_value(property_name, width):
    thickness = max(1, min(200, round(width)))
    # Blender's file versioning converts legacy thickness to this world radius.
    return thickness if property_name == 'thickness' else thickness / 2000.0


def _mark_fields(mesh, domain):
    if domain == 'EDGE':
        return mesh.edges, bpy.types.MeshEdge, 'freestyle_edge'
    if domain == 'FACE':
        return mesh.polygons, bpy.types.MeshPolygon, 'freestyle_face'
    raise ValueError(f'Invalid mark domain: {domain}')


def read_mesh_marks(mesh, domain):
    elements, element_type, attribute_name = _mark_fields(mesh, domain)
    marks = np.zeros(len(elements), dtype=np.bool_)
    if element_type.bl_rna.properties.get('use_freestyle_mark') is not None:
        elements.foreach_get('use_freestyle_mark', marks)
    else:
        attribute = mesh.attributes.get(attribute_name)
        if attribute and attribute.domain == domain and attribute.data_type == 'BOOLEAN':
            attribute.data.foreach_get('value', marks)
    return marks


def write_mesh_marks(mesh, domain, marks):
    elements, element_type, attribute_name = _mark_fields(mesh, domain)
    if element_type.bl_rna.properties.get('use_freestyle_mark') is not None:
        elements.foreach_set('use_freestyle_mark', marks)
    else:
        attribute = mesh.attributes.get(attribute_name)
        if attribute is None:
            attribute = mesh.attributes.new(attribute_name, 'BOOLEAN', domain)
        if attribute.domain != domain or attribute.data_type != 'BOOLEAN':
            raise ValueError(f'{attribute_name} must be a Boolean {domain} attribute')
        attribute.data.foreach_set('value', marks)
