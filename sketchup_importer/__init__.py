# <pep8-80 compliant>

__author__ = 'Martijn Berger'
__license__ = "GPL"

'''
This program is free software; you can redistribute it and
or modify it under the terms of the GNU General Public License
as published by the Free Software Foundation; either version 3
of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program; if not, see http://www.gnu.org/licenses
'''

import math
import os
import shutil
import tempfile
import time
# Added for 3D Warehouse import
import re
import json
import urllib.request
import urllib.error
import wget  # ensure wget is imported unconditionally (still available for fallback if needed)

import bpy
from bpy.props import (BoolProperty, EnumProperty, FloatProperty, IntProperty,
                       StringProperty)
from bpy.types import AddonPreferences, Operator
from bpy_extras.io_utils import (ExportHelper, ImportHelper, unpack_face_list,
                                 unpack_list)
from mathutils import Matrix, Euler, Quaternion, Vector

from . import sketchup
from .SKPutil import *

import bpy.utils.previews as previews

bl_info = {
    "name": "SketchUp Importer",
    "author": "Martijn Berger, Sanjay Mehta, Arindam Mondal, Peter Kirkham",
    "version": (0, 23, 2),
    "blender": (3, 2, 0),
    "description": "Import of native SketchUp (.skp) files",
    #"warning": "Very early preview",
    "wiki_url": "https://github.com/martijnberger/pyslapi",
    "doc_url": "https://github.com/arindam-m/pyslapi/wiki",
    "tracker_url": "https://github.com/arindam-m/pyslapi/wiki/Bug-Report",
    "category": "Import-Export",
    "location": "File > Import"
}

DEBUG = False

LOGS = True

MIN_LOGS = False

if not LOGS:
    MIN_LOGS = True


class SketchupAddonPreferences(AddonPreferences):
    bl_idname = __name__

    camera_far_plane: FloatProperty(
        name="Camera Clip Ends At :",
        default=250,
        unit='LENGTH'
    )

    draw_bounds: IntProperty(
        name="Draw Similar Objects As Bounds When It's Over :",
        default=1000
    )

    warehouse_cookie: StringProperty(
        name="3D Warehouse Cookie",
        description="Paste your 3dwarehouse.sketchup.com Cookie header here for restricted downloads.",
        default=""
    )

    def draw(self, context):
        layout = self.layout
        layout.label(text="- Basic Import Options -")
        row = layout.row()
        row.use_property_split = True
        row.prop(self, 'camera_far_plane')
        layout = self.layout
        row = layout.row()
        row.use_property_split = True
        row.prop(self, 'draw_bounds')
        layout.separator()
        layout.label(text="- 3D Warehouse Download -")
        row = layout.row()
        row.prop(self, 'warehouse_cookie')


def skp_log(*args):
    # Log output by pre-pending "SU |"
    if len(args) > 0:
        print("SU | " + " ".join(["%s" % a for a in args]))


def create_nested_collection(coll_name):
    context = bpy.context
    main_coll_name = "SKP Imported Data"  # data imported into this collection

    # Check if the main import collection exists and create it if missing
    if not bpy.data.collections.get(main_coll_name):
        skp_main_coll = bpy.data.collections.new(main_coll_name)
        context.scene.collection.children.link(skp_main_coll)

    # Check if the named collection being created exists and create if missing
    if not bpy.data.collections.get(coll_name):
        skp_nested_coll = bpy.data.collections.new(coll_name)
        bpy.data.collections[main_coll_name].children.link(skp_nested_coll)

    # Set active layer to the named collection just created
    view_layer_coll = context.view_layer.layer_collection
    main_parent_coll = view_layer_coll.children[main_coll_name]
    coll_set_to_active = main_parent_coll.children[coll_name]
    context.view_layer.active_layer_collection = coll_set_to_active


class SceneImporter():

    def __init__(self):
        self.filepath = "/tmp/untitled.skp"
        self.name_mapping = {}
        self.component_meshes = {}
        self.scene = None
        self.layers_skip = []

    def set_filename(self,
                     filename):
        self.filepath = filename
        self.basepath, self.skp_filename = os.path.split(self.filepath)
        return self  # allow chaining

    #
    # This is the main method to load a SketchUp file into Blender. The method
    # is structured to import the following in order:
    #     1) Import SketchUp scenes and optional last view as cameras.
    #     2) Import the materials in the SketchUp model.
    #     3) Import components in the SketchUp model as a group containing
    #        multiple linked objects if the number of instances for each
    #        component is higher than a given threshold.
    #     4) Import remaining mesh objects.
    #
    def load(self,
             context,
             **options):
        """Load a SketchUp file"""

        # Blender settings
        self.context = context
        self.reuse_material = options['reuse_material']
        self.reuse_group = options['reuse_existing_groups']
        self.max_instance = options['max_instance']
        #self.render_engine = options['render_engine']
        self.component_stats = defaultdict(list)
        self.component_skip = proxy_dict()
        self.component_depth = proxy_dict()
        self.group_written = {}
        ren_res_x = context.scene.render.resolution_x
        ren_res_y = context.scene.render.resolution_y
        self.aspect_ratio = ren_res_x / ren_res_y
        
        # Start stopwatch for overall import
        _time_main = time.time()

        # Log filename being imported
        if LOGS:
            skp_log(f"Importing: {self.filepath}")
        addon_name = __name__.split('.')[0]
        self.prefs = context.preferences.addons[addon_name].preferences

        # Open the SketchUp file and access the model using SketchUp API
        try:
            self.skp_model = sketchup.Model.from_file(self.filepath)
        except Exception as e:
            if LOGS:
                skp_log(f"Error reading input file: {self.filepath}")
                skp_log(e)
            return {'FINISHED'}

        # Start stopwatch for camera import
        if not MIN_LOGS:
            skp_log("")
            skp_log("=== Importing Sketchup scenes and views as Blender "
                    "Cameras ===")
        _time_camera = time.time()

        # Create collection for cameras
        create_nested_collection("SKP Scenes (as Cameras)")

        # Import a specific named SketchUp scene as a Blender camera and hide
        # the layers associated with that specific scene
        if options['import_scene']:
            options['scenes_as_camera'] = False
            options['import_camera'] = True
            for s in self.skp_model.scenes:
                if s.name == options['import_scene']:
                    if not MIN_LOGS:
                        skp_log(f"Importing named SketchUp scene '{s.name}'")
                    self.scene = s

                    # Skip s.layers which are the invisible layers
                    self.layers_skip = [l for l in s.layers]
            if not self.layers_skip and not MIN_LOGS:
                skp_log("Scene: '{}' didn't have any invisible layers."
                        .format(options['import_scene']))
            if self.layers_skip != [] and not MIN_LOGS:
                hidden_layers = sorted([l.name for l in self.layers_skip])
                print("SU | Invisible Layer(s)/Tag(s): \n     ", end="")
                print(*hidden_layers, sep=', ')

        # Import each scene as a Blender camera
        if options['scenes_as_camera']:
            if not MIN_LOGS:
                skp_log("Importing all SketchUp scenes as Blender cameras")
            for s in self.skp_model.scenes:
                self.write_camera(s.camera, s.name)

        # Set the active camera and use for 3D view
        if options['import_camera']:
            if not MIN_LOGS:
                skp_log("Importing last SketchUp view as Blender camera")
            if self.scene:
                active_cam = self.write_camera(self.scene.camera,
                                               name=self.scene.name)
                context.scene.camera = bpy.data.objects[active_cam]
            else:
                active_cam = self.write_camera(self.skp_model.camera)
                context.scene.camera = bpy.data.objects[active_cam]
            for area in bpy.context.screen.areas:
                if area.type == 'VIEW_3D':
                    area.spaces[0].region_3d.view_perspective = 'CAMERA'
                    break
        SKP_util.layers_skip = self.layers_skip
        if not MIN_LOGS:
            skp_log("Cameras imported in "
                    f"{(time.time() - _time_camera):.4f} sec.")

        # Start stopwatch for material imports
        if not MIN_LOGS:
            skp_log("")
            skp_log("=== Importing Sketchup materials into Blender ===")
        _time_material = time.time()
        self.write_materials(self.skp_model.materials)
        if not MIN_LOGS:
            skp_log("Materials imported in "
                    f"{(time.time() - _time_material):.4f} sec.")

        # Start stopwatch for component import
        if not MIN_LOGS:
            skp_log("")
            skp_log("=== Importing Sketchup components into Blender ===")
        _time_analyze_depth = time.time()

        # Create collection for components
        create_nested_collection('SKP Components')

        # Determine the number of components that exist in the SketchUp model
        self.skp_components = proxy_dict(
            self.skp_model.component_definition_as_dict)
        u_comps = [k for k, v in self.skp_components.items()]
        if not MIN_LOGS:
            print(f"SU | Contains {len(u_comps)} components: \n     ", end="")
            print(*u_comps, sep=', ')

        # Analyse component depths
        D = SKP_util()
        for c in self.skp_model.component_definitions:
            self.component_depth[c.name] = D.component_deps(c.entities)
            if DEBUG:
                print(f"     -- ({c.name}) --\n        "
                      f"Depth: {self.component_depth[c.name]}\n", end="")
                print("        Instances (Used): "
                      f"{c.numInstances} ({c.numUsedInstances})")
        if not MIN_LOGS:
            skp_log(f"Component depths analyzed in "
                    f"{(time.time() - _time_analyze_depth):.4f} sec.")

        # Import the components as duplicated groups then hide components
        self.write_duplicateable_groups()
        bpy.data.collections['SKP Components'].hide_viewport = True
        for vl in context.scene.view_layers:
            for l in vl.active_layer_collection.children:
                if l.name == 'SKP Components':
                    l.exclude = True  # hide component collection in view layer
        if options['dedub_only']:
            return {'FINISHED'}

        # Start stopwatch for mesh objects import
        if not MIN_LOGS:
            skp_log("")
            skp_log("=== Importing Sketchup mesh objects into Blender ===")
        _time_mesh_data = time.time()

        # Create collection for mesh objects
        create_nested_collection('SKP Mesh Objects')

        # Import mesh objects into structure that matches the SketchUp outliner
        self.write_entities(self.skp_model.entities,
                            "_(Loose Entity)",
                            Matrix.Identity(4))
        for k, _v in self.component_stats.items():
            name, mat = k
            if options['dedub_type'] == 'VERTEX':
                self.instance_group_dupli_vert(name, mat, self.component_stats)
            else:
                self.instance_group_dupli_face(name, mat, self.component_stats)
        if not MIN_LOGS:
            skp_log("Entities imported in "
                    f"{(time.time() - _time_mesh_data):.4f} sec.")

        # Importing has completed
        if LOGS:
            skp_log("Finished entire importing process in %.4f sec.\n" %
                    (time.time() - _time_main))
        return {'FINISHED'}

    #
    # Write components as groups that can be duplicated later.
    #
    def write_duplicateable_groups(self):
        component_stats = self.analyze_entities(
            self.skp_model.entities,
            "Sketchup",
            Matrix.Identity(4),
            component_stats=defaultdict(list))
        instance_when_over = self.max_instance
        max_depth = max(self.component_depth.values(), default=0)

        # Filter out components from list if the total number of instances
        # is lower than the minimum threshold for creating duplicated mesh
        # objects.
        component_stats = {
            k: v
            for k, v in component_stats.items() if len(v) >= instance_when_over
        }
        for i in range(max_depth + 1):
            for k, v in component_stats.items():
                name, mat = k
                depth = self.component_depth[name]
                comp_def = self.skp_components[name]
                if comp_def and depth == 1:
                    #self.component_skip[(name, mat)] = comp_def.entities
                    pass
                elif comp_def and depth == i:
                    gname = group_name(name, mat)
                    if self.reuse_group and gname in bpy.data.collections:
                        skp_log("Group {} already defined".format(gname))
                        self.component_skip[(name, mat)] = comp_def.entities
                        self.group_written[(name,
                                            mat)] = bpy.data.collections[gname]
                    else:
                        group = bpy.data.collections.new(name=gname)
                        skp_log("Component {} written as group".format(gname))
                        self.component_def_as_group(comp_def.entities,
                                                    name,
                                                    Matrix(),
                                                    default_material=mat,
                                                    etype=EntityType.outer,
                                                    group=group)
                        self.component_skip[(name, mat)] = comp_def.entities
                        self.group_written[(name, mat)] = group

    def analyze_entities(self,
                         entities,
                         name,
                         transform,
                         default_material="Material",
                         etype=EntityType.none,
                         component_stats=None,
                         component_skip=None):
        if component_skip is None:
            component_skip = []
        if etype == EntityType.component:
            component_stats[(name, default_material)].append(transform)
        for group in entities.groups:
            if self.layers_skip and group.layer in self.layers_skip:
                continue
            if DEBUG:
                print(f"     |G {group.name}")
                print(f"     {Matrix(group.transform)}")
            self.analyze_entities(group.entities,
                                  "G-" + group.name,
                                  transform @ Matrix(group.transform),
                                  default_material=inherent_default_mat(
                                      group.material, default_material),
                                  etype=EntityType.group,
                                  component_stats=component_stats)
        for instance in entities.instances:
            if self.layers_skip and instance.layer in self.layers_skip:
                continue
            mat = inherent_default_mat(instance.material, default_material)
            cdef = self.skp_components[instance.definition.name]
            if (cdef.name, mat) in component_skip:
                continue
            if DEBUG:
                print(f"     |C {cdef.name}")
                print(f"     {Matrix(instance.transform)}")
            self.analyze_entities(cdef.entities,
                                  cdef.name,
                                  transform @ Matrix(instance.transform),
                                  default_material=mat,
                                  etype=EntityType.component,
                                  component_stats=component_stats)
        return component_stats

    #
    # Import materials from SketchUp into Blender.
    #
    def write_materials(self,
                        materials):
        if self.context.scene.render.engine != 'CYCLES':
            self.context.scene.render.engine = 'CYCLES'
        self.materials = {}
        self.materials_scales = {}
        if self.reuse_material and 'Material' in bpy.data.materials:
            self.materials['Material'] = bpy.data.materials['Material']
        else:
            bmat = bpy.data.materials.new('Material')
            bmat.diffuse_color = (.8, .8, .8, 1)
            #if self.render_engine == 'CYCLES':
            bmat.use_nodes = True
            self.materials['Material'] = bmat
        for mat in materials:
            name = mat.name
            if mat.texture:
                self.materials_scales[name] = mat.texture.dimensions[2:]
            else:
                self.materials_scales[name] = (1.0, 1.0)
            if self.reuse_material and not name in bpy.data.materials:
                bmat = bpy.data.materials.new(name)
                r, g, b, a = mat.color
                tex = mat.texture
                bmat.diffuse_color = (math.pow((r / 255.0), 2.2),
                                      math.pow((g / 255.0), 2.2),
                                      math.pow((b / 255.0), 2.2),
                                      round((a / 255.0), 2))  # sRGB to Linear

                if round((a / 255.0), 2) < 1:
                    bmat.blend_method = 'BLEND'
                bmat.use_nodes = True
                default_shader = bmat.node_tree.nodes['Principled BSDF']
                default_shader_base_color = default_shader.inputs['Base Color']
                default_shader_base_color.default_value = bmat.diffuse_color
                default_shader_alpha = default_shader.inputs['Alpha']
                default_shader_alpha.default_value = round((a / 255.0), 2)
                if tex:
                    tex_name = tex.name.split("\\")[-1]
                    temp_dir = tempfile.gettempdir()
                    skp_fname = self.filepath.split("\\")[-1].split(".")[0]
                    temp_dir += '\\' + skp_fname
                    if not os.path.isdir(temp_dir):
                        os.mkdir(temp_dir)
                    temp_file_path = os.path.join(temp_dir, tex_name)
                    #skp_log(f"Texture saved temporarily at {temp_file_path}")
                    tex.write(temp_file_path)
                    img = bpy.data.images.load(temp_file_path)
                    img.pack()
                    #os.remove(temp_file_path)
                    shutil.rmtree(temp_dir)
                    #if self.render_engine == 'CYCLES':
                    #    bmat.use_nodes = True
                    tex_node = bmat.node_tree.nodes.new('ShaderNodeTexImage')
                    tex_node.image = img
                    tex_node.location = Vector((-750, 225))
                    bmat.node_tree.links.new(
                        tex_node.outputs['Color'], default_shader_base_color)
                    bmat.node_tree.links.new(
                        tex_node.outputs['Alpha'], default_shader_alpha)
                self.materials[name] = bmat
            else:
                self.materials[name] = bpy.data.materials[name]
            if not MIN_LOGS:
                print(f"     {name}")

    def write_mesh_data(self,
                        entities=None,
                        name="",
                        default_material='Material'):

        mesh_key = (name, default_material)
        if mesh_key in self.component_meshes:
            return self.component_meshes[mesh_key]
        verts = []
        loops_vert_idx = []
        mat_index = []
        smooth = []
        mats = keep_offset()
        seen = keep_offset()
        uv_list = []
        alpha = False
        uvs_used = False

        for f in entities.faces:

            if f.material:
                mat_number = mats[f.material.name]
            else:
                mat_number = mats[default_material]
                if default_material != 'Material':
                    try:
                        f.st_scale = self.materials_scales[default_material]
                    except KeyError as _e:
                        pass

            vs, tri, uvs = f.tessfaces
            num_loops = 0

            mapping = {}
            for i, (v, uv) in enumerate(zip(vs, uvs)):
                l = len(seen)
                mapping[i] = seen[v]
                if len(seen) > l:
                    verts.append(v)
                uvs.append(uv)

            smooth_edge = False

            for edge in f.edges:
                if edge.GetSmooth() == True:
                    smooth_edge = True
                    break

            for face in tri:
                f0, f1, f2 = face[0], face[1], face[2]
                num_loops += 1

                if mapping[f2] == 0:
                    loops_vert_idx.extend([mapping[f2],
                                           mapping[f0],
                                           mapping[f1]])

                    uv_list.append((uvs[f2][0], uvs[f2][1],
                                    uvs[f0][0], uvs[f0][1],
                                    uvs[f1][0], uvs[f1][1]))

                else:
                    loops_vert_idx.extend([mapping[f0],
                                           mapping[f1],
                                           mapping[f2]])

                    uv_list.append((uvs[f0][0], uvs[f0][1],
                                    uvs[f1][0], uvs[f1][1],
                                    uvs[f2][0], uvs[f2][1]))

                smooth.append(smooth_edge)
                mat_index.append(mat_number)

        if len(verts) == 0:
            return None, False

        me = bpy.data.meshes.new(name)

        if len(mats) >= 1:
            mats_sorted = OrderedDict(sorted(mats.items(), key=lambda x: x[1]))
            for k in mats_sorted.keys():
                try:
                    bmat = self.materials[k]
                except KeyError as _e:
                    bmat = self.materials['Material']
                me.materials.append(bmat)
                #if bmat.alpha < 1.0:
                #    alpha = True
                try:
                    #                    if self.render_engine == 'CYCLES':
                    if 'Image Texture' in bmat.node_tree.nodes.keys():
                        uvs_used = True
                #else:
                #    for ts in bmat.texture_slots:
                #        if ts is not None and ts.texture_coords is not
                #                        None:
                #            uvs_used = True
                except AttributeError as _e:
                    uvs_used = False
        else:
            skp_log(f"WARNING: Object {name} has no material!")

        tri_faces = list(zip(*[iter(loops_vert_idx)] * 3))
        tri_face_count = len(tri_faces)

        loop_start = []
        i = 0
        for f in tri_faces:
            loop_start.append(i)
            i += len(f)

        loop_total = list(map(lambda f: len(f), tri_faces))

        me.vertices.add(len(verts))
        me.vertices.foreach_set('co', unpack_list(verts))

        me.loops.add(len(loops_vert_idx))
        me.loops.foreach_set('vertex_index', loops_vert_idx)

        me.polygons.add(tri_face_count)
        me.polygons.foreach_set('loop_start', loop_start)
        me.polygons.foreach_set('loop_total', loop_total)
        me.polygons.foreach_set('material_index', mat_index)
        me.polygons.foreach_set('use_smooth', smooth)

        if uvs_used:
            k, l = 0, 0
            me.uv_layers.new()
            for i in range(len(tri_faces)):
                for j in range(3):
                    uv_cordinates = (uv_list[i][l], uv_list[i][l + 1])
                    me.uv_layers[0].data[k].uv = Vector(uv_cordinates)
                    k += 1
                    if j != 2:
                        l += 2
                    else:
                        l = 0

        me.update(calc_edges=True)
        me.validate()
        self.component_meshes[mesh_key] = me, alpha

        return me, alpha

    #
    # Recursively import all the mesh objects. Groups containing no mesh
    # information are imported as empty objects and can contain nested
    # groups or components. This approach preserves the hierarchy from the
    # SketchUp outliner.
    #
    def write_entities(self,
                       entities,
                       name,
                       parent_transform,
                       default_material='Material',
                       etype=None,
                       parent_name=None,
                       parent_location=Vector((0, 0, 0))):

        # Check if this is a component that has already been duplicated. We
        # can skip writing this if it is already contained in a duplication
        # group.
        if etype == EntityType.component and (
                name, default_material) in self.component_skip:
            self.component_stats[(name,
                                  default_material)].append(parent_transform)
            return

        # Get the mesh data for this object
        me, alpha = self.write_mesh_data(entities=entities, name=name,
                                         default_material=default_material)

        # If there are no further nested groups or components, then we can
        # create an object containing the mesh. Otherwise we create a new
        # empty object and place an object containing the loose geometry as
        # a mesh within this group.
        nested_groups = 0
        for group in entities.groups:
            nested_groups += 1  # count groups (brute force approach)
        nested_comps = 0
        for comp in entities.instances:
            nested_comps += 1  # count components (brute force approach)
        nested_count = nested_groups + nested_comps
        hide_empty = False
        if nested_count == 0 or name == "_(Loose Entity)":
            ob = bpy.data.objects.new(name, me)
            ob.matrix_world = parent_transform
            if 0.01 < alpha < 1.0:
                ob.show_transparent = True
            if me:
                me.update(calc_edges=True)
        else:
            ob = bpy.data.objects.new(name, None)  # empty object to hold group
            ob.matrix_world = parent_transform
            #ob.hide_viewport = True  # disable empties in viewport
            hide_empty = True
            if me:
                ob_mesh = bpy.data.objects.new("_" + name + " (Loose Mesh)",
                                               me)
                ob_mesh.matrix_world = parent_transform
                if 0.01 < alpha < 1.0:
                    ob_mesh.show_transparent = True
                me.update(calc_edges=True)
                ob_mesh.parent = ob
                ob_mesh.location = Vector((0, 0, 0))
                bpy.context.collection.objects.link(ob_mesh)

        # Nested adjustments to the world matrix
        loc = ob.location
        nested_location = Vector((loc[0], loc[1], loc[2]))

        # Nest the object by assigning it to the parent object
        if parent_name is not None and parent_name != "_(Loose Entity)":
            ob.parent = bpy.data.objects[parent_name]
            ob.location -= parent_location
        if nested_count > 0:
            ob.rotation_mode = 'QUATERNION'  # change from default mode of xyz
            ob.rotation_quaternion = Vector((1, 0, 0, 0))
            ob.scale = Vector((1, 1, 1))
        bpy.context.collection.objects.link(ob)
        ob.hide_set(hide_empty)  # enable but do not show empties in viewport

        for group in entities.groups:
            if group.hidden:
                continue
            if self.layers_skip and group.layer in self.layers_skip:
                continue
            temp_ob = bpy.data.objects.new(group.name, None)
            gname = "G-" + group_safe_name(temp_ob.name)
            if DEBUG:
                print(f"     Grp: {gname} in {ob.name}")
            self.write_entities(group.entities,
                                gname,
                                parent_transform @ Matrix(group.transform),
                                default_material=inherent_default_mat(
                                    group.material, default_material),
                                etype=EntityType.group,
                                parent_name=ob.name,
                                parent_location=nested_location)

        for instance in entities.instances:
            if instance.hidden:
                continue
            if self.layers_skip and instance.layer in self.layers_skip:
                continue
            mat_name = inherent_default_mat(instance.material,
                                            default_material)
            cdef = self.skp_components[instance.definition.name]
            if instance.name == "":
                cname = "C-" + cdef.name
            else:
                cname = instance.name + " (C-" + cdef.name + ")"
            if DEBUG:
                print(f"     Cmp: {cname} in {ob.name}")
            self.write_entities(cdef.entities,
                                cname,
                                parent_transform @ Matrix(instance.transform),
                                default_material=mat_name,
                                etype=EntityType.component,
                                parent_name=ob.name,
                                parent_location=nested_location)

    def instance_object_or_group(self,
                                 name,
                                 default_material):
        try:
            group = self.group_written[(name, default_material)]
            ob = bpy.data.objects.new(name=name, object_data=None)
            ob.instance_type = 'COLLECTION'
            ob.instance_collection = group
            ob.empty_display_size = 0.01
            return ob
        except KeyError as _e:
            me, alpha = self.component_meshes[(name, default_material)]
            ob = bpy.data.objects.new(name, me)
            if alpha:
                ob.show_transparent = True
            me.update(calc_edges=True)
            return ob

    def component_def_as_group(self,
                               entities,
                               name,
                               parent_transform,
                               default_material='Material',
                               etype=None,
                               group=None):

        if etype == EntityType.outer:
            if (name, default_material) in self.component_skip:
                return
            else:
                if DEBUG:
                    skp_log("Write instance definition as group {} {}".format(
                        group.name, default_material))
                self.component_skip[(name, default_material)] = True
        if etype == EntityType.component and (
                name, default_material) in self.component_skip:
            ob = self.instance_object_or_group(name, default_material)
            ob.matrix_world = parent_transform
            self.context.collection.objects.link(ob)
            try:
                ob.layers = 18 * [False] + [True] + [False]
            except:
                pass  # capture AttributeError
            group.objects.link(ob)
            return
        else:
            me, alpha = self.write_mesh_data(entities=entities, name=name,
                                             default_material=default_material)
        if me:
            ob = bpy.data.objects.new(name, me)
            ob.matrix_world = parent_transform
            if alpha:
                ob.show_transparent = True
            me.update(calc_edges=True)
            self.context.collection.objects.link(ob)
            try:
                ob.layers = 18 * [False] + [True] + [False]
            except:
                pass  # capture AttributeError
            group.objects.link(ob)
        for g in entities.groups:
            if self.layers_skip and g.layer in self.layers_skip:
                continue
            self.component_def_as_group(
                g.entities,
                "G-" + g.name,
                parent_transform @ Matrix(g.transform),
                default_material=inherent_default_mat(g.material,
                                                      default_material),
                etype=EntityType.group,
                group=group)
        for instance in entities.instances:
            if self.layers_skip and instance.layer in self.layers_skip:
                continue
            cdef = self.skp_components[instance.definition.name]
            self.component_def_as_group(
                cdef.entities,
                cdef.name,
                parent_transform @ Matrix(instance.transform),
                default_material=inherent_default_mat(instance.material,
                                                      default_material),
                etype=EntityType.component,
                group=group)

    #
    # Creates a single group in a collection that contains duplicated
    # instances of a component. Scaling and rotations are used to identify
    # similar components. Each duplicate group contains components with the
    # same scale and rotation applied.
    #
    def instance_group_dupli_vert(self,
                                  name,
                                  default_material,
                                  component_stats):

        def get_orientations(v):
            orientations = defaultdict(list)
            for transform in v:
                loc, rot, scale = Matrix(transform).decompose()
                scale = (scale[0], scale[1], scale[2])
                rot = (rot[0], rot[1], rot[2], rot[3])
                orientations[(scale, rot)].append((loc[0], loc[1], loc[2]))
            for key, locs in orientations.items():
                scale, rot = key
                yield scale, rot, locs

        # Create a new group with duplicated components as a linked object.
        # Each duplicated group has a specific location, scale and rotation
        # applied.
        for scale, rot, locs in get_orientations(
                component_stats[(name, default_material)]):
            verts = []
            main_loc = Vector(locs[0])
            for c in locs:
                verts.append(Vector(c) - main_loc)
            dme = bpy.data.meshes.new("DUPLI-" + name)
            dme.vertices.add(len(verts))
            dme.vertices.foreach_set("co", unpack_list(verts))
            dme.update(calc_edges=True)  # update mesh with new data
            dme.validate()
            dob = bpy.data.objects.new("DUPLI-" + name, dme)
            dob.location = main_loc
            dob.instance_type = 'VERTS'
            ob = self.instance_object_or_group(name, default_material)
            ob.scale = scale
            ob.rotation_mode = 'QUATERNION'  # change from default mode of xyz
            ob.rotation_quaternion = Quaternion((rot[0], rot[1], rot[2],
                                                 rot[3]))
            ob.parent = dob
            self.context.collection.objects.link(ob)
            self.context.collection.objects.link(dob)
            skp_log(f"Complex group {name} {default_material} instanced "
                    f"{len(verts)} times, scale -> {scale}, rot -> {rot}")
        return

    def instance_group_dupli_face(self,
                                  name,
                                  default_material,
                                  component_stats):

        def get_orientations(v):
            orientations = defaultdict(list)
            for transform in v:
                _loc, _rot, scale = Matrix(transform).decompose()
                scale = (scale[0], scale[1], scale[2])
                orientations[scale].append(transform)
            for scale, transforms in orientations.items():
                yield scale, transforms

        for _scale, transforms in get_orientations(
                component_stats[(name, default_material)]):
            main_loc, _real_rot, real_scale = Matrix(transforms[0]).decompose()
            verts = []
            faces = []
            f_count = 0
            for c in transforms:
                l_loc, l_rot, _l_scale = Matrix(c).decompose()
                mat = Matrix.Translation(l_loc) * l_rot.to_matrix().to_4x4()
                verts.append(Vector(
                    (mat * Vector((-0.05, -0.05, 0, 1.0)))[0:3]) - main_loc)
                verts.append(Vector(
                    (mat * Vector((0.05, -0.05, 0, 1.0)))[0:3]) - main_loc)
                verts.append(Vector(
                    (mat * Vector((0.05, 0.05, 0, 1.0)))[0:3]) - main_loc)
                verts.append(Vector(
                    (mat * Vector((-0.05, 0.05, 0, 1.0)))[0:3]) - main_loc)
                faces.append(
                    (f_count + 0, f_count + 1, f_count + 2, f_count + 3))
                f_count += 4
            dme = bpy.data.meshes.new("DUPLI-" + name)
            dme.vertices.add(len(verts))
            dme.vertices.foreach_set('co', unpack_list(verts))
            dme.tessfaces.add(f_count / 4)
            dme.tessfaces.foreach_set('vertices_raw', unpack_face_list(faces))
            dme.update(calc_edges=True)  # Update mesh with new data
            dme.validate()
            dob = bpy.data.objects.new("DUPLI-" + name, dme)
            dob.instance_type = 'FACES'
            dob.location = main_loc
            #dob.use_dupli_faces_scale = True
            #dob.dupli_faces_scale = 10
            ob = self.instance_object_or_group(name, default_material)
            ob.scale = real_scale
            ob.parent = dob
            self.context.collection.objects.link(ob)
            self.context.collection.objects.link(dob)
            skp_log("Complex group {} {} instanced {} times".format(
                name, default_material, f_count / 4))
        return

    def write_camera(self,
                     camera,
                     name="Last View"):
        skp_log(f"Writing camera: {name}")
        pos, target, up = camera.GetOrientation()
        bpy.ops.object.add(type='CAMERA', location=pos)
        ob = self.context.object
        ob.name = "Cam: " + name
        z = (Vector(pos) - Vector(target))
        x = Vector(up).cross(z)
        y = z.cross(x)
        x.normalize()
        y.normalize()
        z.normalize()
        ob.matrix_world.col[0] = x.resized(4)
        ob.matrix_world.col[1] = y.resized(4)
        ob.matrix_world.col[2] = z.resized(4)
        cam = ob.data
        aspect_ratio = camera.aspect_ratio
        fov = camera.fov
        if aspect_ratio == False:
            skp_log(f"Cam: '{name}' uses dynamic/screen aspect ratio.")
            aspect_ratio = self.aspect_ratio
        if fov == False:
            skp_log(f"Cam: '{name}' is in Orthographic Mode.")
            cam.type = 'ORTHO'
        #cam.ortho_scale = 3.0
        else:
            cam.angle = (math.pi * fov / 180) * aspect_ratio
        cam.clip_end = self.prefs.camera_far_plane
        cam.name = "Cam: " + name
        return cam.name


class SceneExporter():

    def __init__(self):
        self.filepath = '/tmp/untitled.skp'

    def set_filename(self, filename):
        self.filepath = filename
        self.basepath, self.skp_filename = os.path.split(self.filepath)
        return self

    def save(self, context, **options):
        skp_log(f"Finished exporting: {self.filepath}")
        return {'FINISHED'}


class ImportSKP(Operator, ImportHelper):
    """Load a Trimble SketchUp .skp file"""
    bl_idname = 'import_scene.skp'
    bl_label = "Import SKP"
    bl_options = {'PRESET', 'REGISTER', 'UNDO'}
    filename_ext = '.skp'

    filter_glob: StringProperty(
        default="*.skp",
        options={'HIDDEN'},
    )

    scenes_as_camera: BoolProperty(
        name="Scene(s) As Camera(s)",
        description="Import SketchUp Scenes As Blender Camera.",
        default=True
    )

    import_camera: BoolProperty(
        name="Last View In SketchUp As Camera View",
        description="Import last saved view in SketchUp as a Blender Camera.",
        default=False
    )

    reuse_material: BoolProperty(
        name="Use Existing Materials",
        description="Doesn't copy material IDs already in the Blender Scene.",
        default=True
    )

    dedub_only: BoolProperty(
        name="Groups Only",
        description="Import instantiated groups only.",
        default=False
    )

    reuse_existing_groups: BoolProperty(
        name="Reuse Groups",
        description="Use existing Blender groups to instance components with.",
        default=False
    )

    # Altered from initial default of 50 so as to force import all
    # components to be imported as duplicated objects.
    max_instance: IntProperty(
        name="Instantiation Threshold :",
        default=1
    )

    dedub_type: EnumProperty(
        name="Instancing Type :",
        items=(('FACE', "Faces", ""),
               ('VERTEX', "Vertices", ""),),
        default='VERTEX',
    )

    import_scene: StringProperty(
        name="Import A Scene :",
        description="Import a specific SketchUp Scene",
        default=""
    )

    def execute(self,
                context):
        keywords = self.as_keywords(ignore=("axis_forward", "axis_up",
                                            "filter_glob", "split_mode"))
        return SceneImporter().set_filename(keywords['filepath']).load(
            context, **keywords)

    def draw(self,
             context):
        layout = self.layout
        layout.label(text="- Primary Import Options -")
        row = layout.row()
        row.prop(self, "scenes_as_camera")
        row = layout.row()
        row.prop(self, "import_camera")
        row = layout.row()
        row.prop(self, "reuse_material")
        row = layout.row()
        row.prop(self, "dedub_only")
        row = layout.row()
        row.prop(self, "reuse_existing_groups")
        col = layout.column()
        col.label(text="- Instantiate components, if they are more than -")
        #split = col.split(factor=0.5)
        #col = split.column()
        col.prop(self, "max_instance")
        row = layout.row()
        row.use_property_split = True
        row.prop(self, "dedub_type")
        row = layout.row()
        row.use_property_split = True
        row.prop(self, "import_scene")


class ExportSKP(Operator, ExportHelper):
    """Export .blend into .skp file"""
    bl_idname = "export_scene.skp"
    bl_label = "Export SKP"
    bl_options = {'PRESET', 'UNDO'}
    filename_ext = ".skp"

    def execute(self,
                context):
        keywords = self.as_keywords()
        return SceneExporter().set_filename(keywords['filepath']) \
            .save(context, **keywords)


class ImportSketchupWarehouseGLB(Operator):
    """Import a SketchUp 3D Warehouse model via its URL.
    Attempts to download latest SKP (highest sXX). Falls back to GLB if SKP is restricted (401) and fallback enabled.
    """
    bl_idname = 'import_scene.skp_warehouse_glb'
    bl_label = 'Import SketchUp 3D Warehouse (.skp/.glb)'
    bl_options = {'REGISTER', 'UNDO'}

    warehouse_url: StringProperty(
        name="3D Warehouse URL",
        description="URL like https://3dwarehouse.sketchup.com/model/{model_id}/{model_name} or direct download-warehouse URL",
        default=""
    )
    fallback_to_glb: BoolProperty(
        name="Fallback to GLB if SKP restricted",
        description="If SKP binary download returns 401, try GLB version",
        default=True
    )
    direct_download_url: StringProperty(
        name="Direct Download URL (optional)",
        description="Paste a direct download-warehouse.sketchup.com URL to override version selection.",
        default=""
    )

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, 'warehouse_url')
        layout.prop(self, 'direct_download_url')
        layout.prop(self, 'fallback_to_glb')

    @staticmethod
    def _extract_model_id(url: str):
        pattern = r'https?://3dwarehouse\.sketchup\.com/model/([0-9a-fA-F\-]{30,36})/'
        m = re.match(pattern, url.strip())
        if not m:
            return None
        return m.group(1)

    @staticmethod
    def _fetch_json(model_id: str):
        api_url = f'https://3dwarehouse.sketchup.com/warehouse/v1.0/entities/{model_id}'
        req = urllib.request.Request(api_url, headers={'User-Agent': 'Blender-SKP-Importer'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                raise RuntimeError(f'HTTP {resp.status} while fetching entity JSON')
            data = resp.read().decode('utf-8', errors='replace')
            return json.loads(data)

    @staticmethod
    def _extract_latest_skp_versions(data: dict):
        binaries = data.get('binaries', {})
        versions = []
        for key in binaries.keys():
            m = re.fullmatch(r's(\d{1,2})', key)
            if m:
                try:
                    versions.append(int(m.group(1)))
                except ValueError:
                    pass
        versions.sort(reverse=True)
        return versions

    @staticmethod
    def _build_skp_url(model_id: str, version_num: int):
        return f"https://3dwarehouse.sketchup.com/warehouse/v1.0/entities/{model_id}/binaries/s{version_num}?download=true"

    @staticmethod
    def _attempt_download(urls, model_id: str, version_key: str, cookie: str = ""):
        last_error = None
        temp_dir = tempfile.mkdtemp(prefix='skp_wh_')
        file_path = os.path.join(temp_dir, f'{model_id}_{version_key}.skp')
        ua = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/127.0.0.1 Safari/537.36')
        base_headers_primary = {
            'User-Agent': ua,
            'Accept': 'application/octet-stream,application/vnd.sketchup.skp,*/*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': 'https://3dwarehouse.sketchup.com/',
            'Connection': 'keep-alive',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'same-origin'
        }
        alt_headers = {
            'User-Agent': ua,
            'Accept': '*/*',
            'Referer': 'https://3dwarehouse.sketchup.com/',
        }
        if cookie:
            base_headers_primary['Cookie'] = cookie
            alt_headers['Cookie'] = cookie
        for u in urls:
            if not u:
                continue
            for attempt, headers in enumerate((base_headers_primary, alt_headers), start=1):
                skp_log(f"Attempt {attempt} (urllib) SKP {version_key}: {u}")
                try:
                    if os.path.exists(file_path):
                        try: os.remove(file_path)
                        except Exception: pass
                    req = urllib.request.Request(u, headers=headers, method='GET')
                    with urllib.request.urlopen(req, timeout=180) as resp, open(file_path, 'wb') as f:
                        shutil.copyfileobj(resp, f)
                    size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
                    if size == 0:
                        raise IOError('Empty file downloaded')
                    skp_log(f"Downloaded (urllib) {version_key} -> {file_path} ({size} bytes)")
                    return file_path, None
                except urllib.error.HTTPError as he:
                    skp_log(f"HTTPError (urllib) {version_key} [{he.code}]: {he.reason}")
                    last_error = he
                    if he.code in (301,302,303,307,308):
                        # Redirect handled automatically, continue
                        continue
                    if he.code == 401:
                        # try next header set or next URL
                        continue
                    # Other HTTP errors: break to next URL
                    break
                except Exception as e:
                    skp_log(f"Error (urllib) {version_key}: {e}")
                    last_error = e
                    # Try alt headers then move on
                    continue
            # Fallback to wget after urllib failures
            skp_log(f"Falling back to wget for {version_key}: {u}")
            try:
                if os.path.exists(file_path):
                    try: os.remove(file_path)
                    except Exception: pass
                # wget does not support cookies directly, so only use if no cookie
                if cookie:
                    skp_log("Skipping wget fallback due to cookie usage.")
                    continue
                wget.download(u, out=file_path, bar=None)
                size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
                if size == 0:
                    raise IOError('Empty file downloaded (wget)')
                skp_log(f"Downloaded (wget) {version_key} -> {file_path} ({size} bytes)")
                return file_path, None
            except Exception as e:
                skp_log(f"Error (wget) {version_key}: {e}")
                last_error = e
                try:
                    if os.path.exists(file_path):
                        os.remove(file_path)
                except Exception:
                    pass
                continue
        return None, last_error

    @staticmethod
    def _download_glb(glb_url: str, model_id: str):
        skp_log(f"Attempting GLB download: {glb_url}")
        req = urllib.request.Request(glb_url, headers={'User-Agent': 'Blender-SKP-Importer'})
        temp_dir = tempfile.mkdtemp(prefix='skp_wh_')
        glb_path = os.path.join(temp_dir, f'{model_id}.glb')
        with urllib.request.urlopen(req, timeout=120) as resp, open(glb_path, 'wb') as f:
            shutil.copyfileobj(resp, f)
        skp_log(f"Downloaded GLB to: {glb_path}")
        return glb_path

    def execute(self, context):
        url = self.warehouse_url
        direct_url = self.direct_download_url.strip()
        if not url and not direct_url:
            self.report({'ERROR'}, 'No URL provided')
            return {'CANCELLED'}
        model_id = self._extract_model_id(url) if url else None
        # Get cookie from preferences
        prefs = context.preferences.addons[__name__.split('.')[0]].preferences
        cookie = prefs.warehouse_cookie.strip() if hasattr(prefs, 'warehouse_cookie') else ""
        # If direct download URL is provided, use it only
        if direct_url:
            skp_log(f"Direct download override: {direct_url}")
            skp_path, err = self._attempt_download([direct_url], model_id or "direct", "direct", cookie)
            if skp_path:
                try:
                    bpy.ops.import_scene.skp(filepath=skp_path)
                    self.report({'INFO'}, f'Imported SKP (direct URL)')
                    return {'FINISHED'}
                except Exception as e:
                    self.report({'ERROR'}, f'Failed importing SKP: {e}')
                    return {'CANCELLED'}
            else:
                self.report({'ERROR'}, f'Failed downloading SKP (direct): {err}')
                return {'CANCELLED'}
        if not model_id:
            self.report({'ERROR'}, 'Could not parse model id from URL')
            return {'CANCELLED'}
        try:
            data = self._fetch_json(model_id)
        except Exception as e:
            self.report({'ERROR'}, f'Failed to fetch entity JSON: {e}')
            return {'CANCELLED'}

        # Gather version numbers (regenerated URLs) and also original JSON urls for fallback
        version_nums = self._extract_latest_skp_versions(data)
        binaries = data.get('binaries', {})
        # Build ordered list of download attempts: regenerated sXX first, then JSON provided url/contentUrl for each sXX
        attempt_map = []
        for v in version_nums:
            regen_url = self._build_skp_url(model_id, v)
            attempt_map.append((f's{v}', [regen_url]))
        for key, val in binaries.items():
            if re.fullmatch(r's(\d{1,2})', key) and isinstance(val, dict):
                json_urls = []
                if val.get('url'): json_urls.append(val['url'])
                if val.get('contentUrl') and val['contentUrl'] not in json_urls:
                    json_urls.append(val['contentUrl'])
                if json_urls:
                    attempt_map.append((key, json_urls))

        glb_url = None
        glb_entry = binaries.get('glb') if isinstance(binaries.get('glb'), dict) else None
        if glb_entry:
            glb_url = glb_entry.get('url') or glb_entry.get('contentUrl')

        skp_imported = False
        last_err = None
        tried_versions = set()

        for version_key, urls in attempt_map:
            if version_key in tried_versions:
                continue
            tried_versions.add(version_key)
            skp_path, err = self._attempt_download(urls, model_id, version_key, cookie)
            if skp_path:
                try:
                    bpy.ops.import_scene.skp(filepath=skp_path)
                    self.report({'INFO'}, f'Imported SKP {version_key}')
                    skp_imported = True
                    break
                except Exception as e:
                    last_err = e
                    continue
            else:
                last_err = err
        if skp_imported:
            return {'FINISHED'}

        if not skp_imported and self.fallback_to_glb and glb_url:
            try:
                glb_path = self._download_glb(glb_url, model_id)
                try:
                    if 'io_scene_gltf2' not in bpy.context.preferences.addons:
                        bpy.ops.preferences.addon_enable(module='io_scene_gltf2')
                except Exception:
                    pass
                bpy.ops.import_scene.gltf(filepath=glb_path)
                self.report({'WARNING'}, f'SKP unavailable (last error: {last_err}); imported GLB fallback')
                return {'FINISHED'}
            except Exception as e:
                self.report({'ERROR'}, f'Failed fallback GLB import: {e}')
                return {'CANCELLED'}

        if last_err:
            self.report({'ERROR'}, f'Failed downloading/importing SKP (last error: {last_err})')
        else:
            self.report({'ERROR'}, 'No SKP versions found')
        return {'CANCELLED'}


# Global preview collection and result cache for 3D Warehouse browser
_skp_wh_previews = None
_skp_wh_results = []  # list of dicts: {model_id, model_name, model_url, icon_id}
_skp_wh_last_query = ''  # track last query to reset offset when changed
_skp_wh_enum_items = []  # dynamic enum items for gallery view
_skp_wh_result_map = {}  # id -> result dict


def _skp_wh_get_prefs():
    addon_name = __name__.split('.')[0]
    return bpy.context.preferences.addons[addon_name].preferences if addon_name in bpy.context.preferences.addons else None


def _skp_wh_ensure_previews():
    global _skp_wh_previews
    if _skp_wh_previews is None:
        _skp_wh_previews = previews.new()
    return _skp_wh_previews


def _skp_wh_clear_previews():
    global _skp_wh_previews, _skp_wh_results
    if _skp_wh_previews:
        for name in list(_skp_wh_previews.keys()):
            try:
                _skp_wh_previews.remove(name)
            except Exception:
                pass
    _skp_wh_results = []


# Properties for search
bpy.types.WindowManager.skp_wh_query = StringProperty(name="Search", default="chair")
if not hasattr(bpy.types.WindowManager, 'skp_wh_offset'):
    bpy.types.WindowManager.skp_wh_offset = IntProperty(name="Offset", default=0, min=0)
# New UI tuning properties for thumbnail size
if not hasattr(bpy.types.WindowManager, 'skp_wh_thumb_cols'):
    bpy.types.WindowManager.skp_wh_thumb_cols = IntProperty(name="Cols", default=2, min=1, max=6, description="Number of thumbnail columns")
if not hasattr(bpy.types.WindowManager, 'skp_wh_thumb_scale'):
    bpy.types.WindowManager.skp_wh_thumb_scale = FloatProperty(name="Scale", default=2.0, min=0.5, max=4.0, description="Thumbnail scale factor (grid mode)")
if not hasattr(bpy.types.WindowManager, 'skp_wh_thumb_mode'):
    bpy.types.WindowManager.skp_wh_thumb_mode = EnumProperty(name="Mode", items=[('GRID','Grid','Grid thumbnails'),('GALLERY','Gallery','Large gallery thumbnails')], default='GALLERY')
# Enum for gallery selection
if not hasattr(bpy.types.WindowManager, 'skp_wh_selected'):
    bpy.types.WindowManager.skp_wh_selected = EnumProperty(name="Model", items=lambda self, ctx: _skp_wh_enum_items, description="Selected 3D Warehouse model")


class SKPWH_OT_Search(Operator):
    bl_idname = 'skp_wh.search'
    bl_label = 'Search 3D Warehouse'
    bl_description = 'Search SketchUp 3D Warehouse and list results'
    bl_options = {'INTERNAL'}

    max_results: IntProperty(name='Max Results', default=24, min=1, max=96)
    page_delta: IntProperty(name='Page Delta', default=0)  # -1 prev, +1 next

    def _build_api_url(self, query: str, offset: int) -> str:
        from urllib.parse import quote
        base = ('https://embed-3dwarehouse.sketchup.com/warehouse/v1.0/entities'
                '?sortBy=relevance%20desc&personalizeSearch=true&personalizeSearchAlgorithm=heuristic'
                '&contentType=3dw&showBinaryAttributes=true&showBinaryMetadata=true&showAttributes=true'
                '&show=all&recordEvent=false&fq=binaryNames%3Dexists%3Dtrue')
        return f"{base}&q={quote(query)}&offset={offset}"

    def _parse_entities(self, data):
        # Now include 'entries' key from sample JSON
        if isinstance(data, dict):
            for k in ('entries', 'entities', 'items', 'results'):
                v = data.get(k)
                if isinstance(v, list) and v:
                    return v
        if isinstance(data, list):
            return data
        return []

    def _slugify(self, name: str):
        import re
        slug = re.sub(r'[^a-zA-Z0-9\- _]+', '', (name or 'Model')).strip().replace(' ', '-')
        return slug[:60] if slug else 'Model'

    def _pick_thumbnail_binary(self, binaries: dict):
        # prefer large webp/jpg then small then tiny; avoid *_ao unless nothing else
        primary_order = ['bot_lt_wp', 'bot_lt', 'bot_st_wp', 'bot_st', 'bot_tt_wp', 'bot_tt']
        ao_order = ['bot_lt_wp_ao', 'bot_lt_ao', 'bot_st_wp_ao', 'bot_st_ao', 'bot_tt_wp_ao', 'bot_tt_ao']
        for key in primary_order + ao_order:
            entry = binaries.get(key)
            if isinstance(entry, dict):
                url = entry.get('url') or entry.get('contentUrl')
                if url:
                    return url, entry.get('originalFileName', '')
        # fallback any image-like ext
        for k, entry in binaries.items():
            if isinstance(entry, dict):
                ext = (entry.get('ext') or '').lower()
                if ext in ('jpg', 'jpeg', 'png', 'webp'):
                    url = entry.get('url') or entry.get('contentUrl')
                    if url:
                        return url, entry.get('originalFileName', '')
        return '', ''

    def execute(self, context):
        global _skp_wh_last_query
        query = context.window_manager.skp_wh_query.strip()
        if not query:
            self.report({'WARNING'}, 'Empty query')
            return {'CANCELLED'}
        wm = context.window_manager
        if query != _skp_wh_last_query:
            wm.skp_wh_offset = 0
            _skp_wh_last_query = query
        if self.page_delta != 0:
            wm.skp_wh_offset = max(0, wm.skp_wh_offset + (self.page_delta * self.max_results))
        offset = wm.skp_wh_offset
        prefs = _skp_wh_get_prefs()
        cookie = prefs.warehouse_cookie.strip() if prefs and getattr(prefs, 'warehouse_cookie', '') else ''
        import urllib.request, urllib.error, json, tempfile, os, shutil, re
        api_url = self._build_api_url(query, offset)
        skp_log(f"Warehouse API search URL: {api_url}")
        headers = {
            'User-Agent': 'Mozilla/5.0',
            'Accept': 'application/json',
            'Referer': 'https://3dwarehouse.sketchup.com/',
        }
        if cookie:
            headers['Cookie'] = cookie
        data = None
        try:
            with urllib.request.urlopen(urllib.request.Request(api_url, headers=headers), timeout=30) as resp:
                raw = resp.read().decode('utf-8', errors='replace')
            data = json.loads(raw)
        except Exception as e:
            self.report({'ERROR'}, f'API search failed: {e}')
            return {'CANCELLED'}
        entries = self._parse_entities(data)
        if not entries:
            _skp_wh_clear_previews()
            self.report({'INFO'}, 'No models found')
            return {'FINISHED'}
        entries = entries[:self.max_results]
        _skp_wh_clear_previews()
        pcoll = _skp_wh_ensure_previews()
        import tempfile
        temp_dir = tempfile.mkdtemp(prefix='skp_wh_thumbs_')
        for ent in entries:
            mid = ent.get('id')
            name = ent.get('title') or ent.get('name') or 'Model'
            slug = self._slugify(name)
            model_url = f"https://3dwarehouse.sketchup.com/model/{mid}/{slug}" if mid else ''
            binaries = ent.get('binaries') if isinstance(ent.get('binaries'), dict) else {}
            # Collect skp versions
            skp_versions = []
            for bname in (ent.get('binaryNames') or []):
                if isinstance(bname, str) and re.fullmatch(r's\d+', bname):
                    try:
                        skp_versions.append(int(bname[1:]))
                    except ValueError:
                        pass
            skp_versions.sort(reverse=True)
            # Determine restricted (all available sXX have /restricted/ in contentUrl)
            restricted = False
            if skp_versions:
                restricted_flags = []
                for v in skp_versions:
                    key = f's{v}'
                    binfo = binaries.get(key)
                    if isinstance(binfo, dict):
                        c_url = binfo.get('contentUrl') or ''
                        restricted_flags.append('/restricted/' in c_url)
                if restricted_flags and all(restricted_flags):
                    restricted = True
            # Polygon count
            poly_count = None
            try:
                poly_count = ent.get('attributes', {}).get('skp', {}).get('polygons', {}).get('value')
            except Exception:
                poly_count = None
            # File size from highest version skp binary
            file_size = None
            skp_filename = ''
            if skp_versions:
                for v in skp_versions:  # first (highest) available
                    key = f's{v}'
                    binfo = binaries.get(key)
                    if isinstance(binfo, dict):
                        file_size = binfo.get('fileSize')
                        skp_filename = binfo.get('originalFileName', '')
                        if file_size:
                            break
            def _fmt_size(num):
                if not num:
                    return '—'
                for unit in ('B','KB','MB','GB'):
                    if num < 1024 or unit == 'GB':
                        return f"{num:.1f}{unit}" if unit != 'B' else f"{num}B"
                    num /= 1024.0
            thumb_url, original_name = self._pick_thumbnail_binary(binaries)
            icon_id = 0
            if thumb_url:
                try:
                    # enforce extension
                    ext = os.path.splitext(thumb_url.split('?',1)[0])[1]
                    if ext.lower() not in ('.jpg', '.jpeg', '.png', '.webp'):
                        ext = '.jpg'
                    thumb_path = os.path.join(temp_dir, f"{mid}{ext}")
                    with urllib.request.urlopen(urllib.request.Request(thumb_url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=20) as ir, open(thumb_path, 'wb') as outf:
                        shutil.copyfileobj(ir, outf)
                    rel_name = os.path.basename(thumb_path)
                    pcoll.load(rel_name, thumb_path, 'IMAGE')
                    icon_id = pcoll[rel_name].icon_id
                except Exception:
                    icon_id = 0
            _skp_wh_results.append({
                'model_id': mid,
                'model_name': slug,
                'display_name': name,
                'model_url': model_url,
                'icon_id': icon_id,
                'skp_versions': skp_versions,
                'restricted': restricted,
                'has_glb': 'glb' in binaries,
                'poly_count': poly_count,
                'file_size': file_size,
                'file_size_fmt': _fmt_size(file_size),
                'skp_filename': skp_filename or ent.get('title') or ''
            })
            # Build enum item (identifier must be unique); use model_id
            if mid:
                _skp_wh_result_map[mid] = _skp_wh_results[-1]
        self.report({'INFO'}, f"Found {len(_skp_wh_results)} models (offset {offset})")
        # Rebuild enum items after results
        global _skp_wh_enum_items
        _skp_wh_enum_items = []
        for r in _skp_wh_results:
            if r['model_id']:
                name_disp = (r['display_name'][:32] + ('…' if len(r['display_name'])>32 else ''))
                _skp_wh_enum_items.append((r['model_id'], name_disp, r['model_url'], r['icon_id'], len(_skp_wh_enum_items)))
        return {'FINISHED'}


class SKPWH_OT_ImportResult(Operator):
    bl_idname = 'skp_wh.import_result'
    bl_label = 'Import Selected 3D Warehouse Model'
    bl_description = 'Import this model using the SKP importer'
    bl_options = {'INTERNAL', 'UNDO'}

    model_id: StringProperty()
    model_name: StringProperty()

    def execute(self, context):
        if not self.model_id:
            return {'CANCELLED'}
        base_url = f"https://3dwarehouse.sketchup.com/model/{self.model_id}/{self.model_name or 'Model'}"
        try:
            bpy.ops.import_scene.skp_warehouse_glb(warehouse_url=base_url)
        except Exception as e:
            self.report({'ERROR'}, f'Failed to start import: {e}')
            return {'CANCELLED'}
        return {'FINISHED'}


class SKPWH_OT_ImportSelected(Operator):
    bl_idname = 'skp_wh.import_selected'
    bl_label = 'Import Selected'
    bl_description = 'Import the selected gallery model'
    bl_options = {'INTERNAL','UNDO'}
    def execute(self, context):
        wm = context.window_manager
        mid = wm.skp_wh_selected
        if not mid or mid not in _skp_wh_result_map:
            self.report({'WARNING'}, 'Nothing selected')
            return {'CANCELLED'}
        res = _skp_wh_result_map[mid]
        try:
            bpy.ops.skp_wh.import_result('INVOKE_DEFAULT', model_id=res['model_id'], model_name=res['model_name'])
        except Exception as e:
            self.report({'ERROR'}, f'Import failed: {e}')
            return {'CANCELLED'}
        return {'FINISHED'}


class VIEW3D_PT_SketchupWarehouseBrowser(bpy.types.Panel):
    bl_label = '3D Warehouse'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'SketchUp'

    def draw(self, context):
        layout = self.layout
        wm = context.window_manager
        row = layout.row()
        row.prop(wm, 'skp_wh_query', text='')
        nav = layout.row(align=True)
        nav.operator('skp_wh.search', text='', icon='VIEWZOOM').page_delta = 0
        nav.operator('skp_wh.search', text='', icon='TRIA_LEFT').page_delta = -1
        nav.operator('skp_wh.search', text='', icon='TRIA_RIGHT').page_delta = 1
        mode_row = layout.row(align=True)
        mode_row.prop(wm, 'skp_wh_thumb_mode', expand=True)
        if wm.skp_wh_thumb_mode == 'GRID':
            size_row = layout.row(align=True)
            size_row.prop(wm, 'skp_wh_thumb_cols', text='Cols')
            size_row.prop(wm, 'skp_wh_thumb_scale', text='Scale')
        layout.label(text=f"Offset: {wm.skp_wh_offset}")
        if not _skp_wh_results:
            layout.label(text='No results')
            return
        if wm.skp_wh_thumb_mode == 'GALLERY':
            layout.template_icon_view(wm, 'skp_wh_selected', show_labels=True)
            # Details of selection
            sel = wm.skp_wh_selected
            if sel and sel in _skp_wh_result_map:
                r = _skp_wh_result_map[sel]
                box = layout.box()
                box.label(text=r.get('display_name',''))
                stats = []
                if r.get('skp_versions'):
                    stats.append('s'+str(r['skp_versions'][0]))
                if r.get('file_size_fmt'):
                    stats.append(r['file_size_fmt'])
                if r.get('poly_count') is not None:
                    stats.append(f"{r['poly_count']} tris")
                box.label(text=' | '.join(stats) if stats else '—')
                if r.get('restricted'):
                    box.label(text='Restricted model (cookie may be required)', icon='LOCKED')
                elif not r.get('skp_versions') and r.get('has_glb'):
                    box.label(text='GLB only (no SKP versions)', icon='FILE_3D')
                box.operator('skp_wh.import_selected', icon='IMPORT')
        else:
            cols = max(1, wm.skp_wh_thumb_cols)
            grid = layout.grid_flow(columns=cols, even_columns=True, even_rows=True, align=True)
            scale = wm.skp_wh_thumb_scale
            for item in _skp_wh_results:
                col = grid.column()
                icon_col = col.column()
                icon_col.scale_x = scale
                icon_col.scale_y = scale
                title = item.get('display_name','')
                if item['icon_id']:
                    op = icon_col.operator('skp_wh.import_result', text='', icon_value=item['icon_id'])
                else:
                    op = icon_col.operator('skp_wh.import_result', text='Import')
                op.model_id = item['model_id']
                op.model_name = item['model_name']
                info_col = col.column(align=True)
                name_line = (title[:40] + ('…' if len(title) > 40 else ''))
                info_col.label(text=name_line)
                stats = []
                if item.get('skp_versions'):
                    stats.append(f"s{item['skp_versions'][0]}")
                if item.get('file_size_fmt'):
                    stats.append(item['file_size_fmt'])
                if item.get('poly_count') is not None:
                    stats.append(f"{item['poly_count']} tris")
                info_col.label(text=' | '.join(stats) if stats else '—')
                flags_row = info_col.row(align=True)
                if item.get('restricted'):
                    flags_row.label(text='Restricted', icon='LOCKED')
                elif not item.get('skp_versions') and item.get('has_glb'):
                    flags_row.label(text='GLB only', icon='FILE_3D')


classes_to_register_extra = [
    SKPWH_OT_Search,
    SKPWH_OT_ImportResult,
    SKPWH_OT_ImportSelected,
    VIEW3D_PT_SketchupWarehouseBrowser,
]


def menu_func_import(self,
                     context):
    self.layout.operator(ImportSKP.bl_idname,
                         text="SketchUp (.skp)")
    # Added menu entry for 3D Warehouse SKP
    self.layout.operator(ImportSketchupWarehouseGLB.bl_idname,
                         text="SketchUp 3D Warehouse (.skp)")


def menu_func_export(self,
                     context):
    self.layout.operator(ExportSKP.bl_idname,
                         text="SketchUp (.skp)")


def register():
    bpy.utils.register_class(SketchupAddonPreferences)
    bpy.utils.register_class(ImportSKP)
    bpy.utils.register_class(ImportSketchupWarehouseGLB)
    for c in classes_to_register_extra:
        bpy.utils.register_class(c)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    # bpy.utils.register_class(ExportSKP)
    # bpy.types.TOPBAR_MT_file_export.append(menu_func_export)


def unregister():
    bpy.utils.unregister_class(ImportSKP)
    bpy.utils.unregister_class(ImportSketchupWarehouseGLB)
    for c in reversed(classes_to_register_extra):
        bpy.utils.unregister_class(c)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    # bpy.utils.unregister_class(ExportSKP)
    # bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)
    bpy.utils.unregister_class(SketchupAddonPreferences)
    _skp_wh_clear_previews()
    global _skp_wh_previews
    if _skp_wh_previews:
        previews.remove(_skp_wh_previews)
        _skp_wh_previews = None
