"""Blender API names shared by the supported preview versions."""
import bpy


def grease_pencil_data():
    collection = getattr(bpy.data, 'grease_pencils_v3', None)
    return collection if collection is not None else bpy.data.grease_pencils
