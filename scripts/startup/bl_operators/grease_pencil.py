# SPDX-FileCopyrightText: 2025 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

import bpy
from bpy.types import Operator
from bpy.props import (
    EnumProperty,
    BoolProperty,
)
from mathutils import Matrix, Vector
import numpy as np


class GREASE_PENCIL_OT_relative_layer_mask_add(Operator):
    """Mask active layer with layer above or below"""

    bl_idname = "grease_pencil.relative_layer_mask_add"
    bl_label = "Mask with Layer Above/Below"
    bl_options = {'REGISTER', 'UNDO'}

    mode: EnumProperty(
        name="Mode",
        items=(
            ('ABOVE', "Above", ""),
            ('BELOW', "Below", "")
        ),
        description="Which relative layer (above or below) to use as a mask",
        default='ABOVE',
    )

    @classmethod
    def poll(cls, context):
        return (
            (obj := context.active_object) is not None and
            obj.is_editable and
            obj.type == 'GREASEPENCIL' and
            obj.data.layers.active is not None and
            obj.data.is_editable
        )

    def execute(self, context):
        obj = context.active_object
        active_layer = obj.data.layers.active

        if self.mode == 'ABOVE':
            masking_layer = active_layer.next_node
        elif self.mode == 'BELOW':
            masking_layer = active_layer.prev_node

        if masking_layer is None or type(masking_layer) != bpy.types.GreasePencilLayer:
            self.report({'ERROR'}, "No layer found")
            return {'CANCELLED'}

        if masking_layer.name in active_layer.mask_layers:
            self.report({'ERROR'}, "Layer is already added as a mask")
            return {'CANCELLED'}

        bpy.ops.grease_pencil.layer_mask_add(name=masking_layer.name)
        active_layer.use_masks = True
        return {'FINISHED'}


class GREASE_PENCIL_OT_perspective_warp(Operator):
    """Warp active stroke using perspective"""

    bl_idname = "grease_pencil.perspective_warp"
    bl_label = "Perspective Warp"
    bl_options = {'REGISTER', 'UNDO'}

    _handles = None
    _src_bbox = None
    _confirm: BoolProperty(default=False, options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if obj is None or obj.type != 'GREASEPENCIL':
            return False
        layer = obj.data.layers.active
        return layer is not None and obj.data.is_editable

    def invoke(self, context, event):
        obj = context.active_object
        layer = obj.data.layers.active
        frame = layer.frames.active

        if frame is None or len(frame.strokes) == 0:
            self.report({'ERROR'}, "No stroke available")
            return {'CANCELLED'}

        if len(frame.strokes) > 1:
            self.report({'ERROR'}, "Only one stroke allowed per layer")
            return {'CANCELLED'}

        stroke = frame.strokes[0]

        min_x = min(p.co.x for p in stroke.points)
        max_x = max(p.co.x for p in stroke.points)
        min_y = min(p.co.y for p in stroke.points)
        max_y = max(p.co.y for p in stroke.points)
        self._src_bbox = [
            (min_x, min_y),
            (max_x, min_y),
            (max_x, max_y),
            (min_x, max_y),
        ]

        self._handles = []
        for co in self._src_bbox:
            empty = bpy.data.objects.new("WarpHandle", None)
            empty.empty_display_size = 0.1
            empty.empty_display_type = 'PLAIN_AXES'
            empty.location = (co[0], co[1], 0)
            context.collection.objects.link(empty)
            self._handles.append(empty)

        context.view_layer.update()
        context.window_manager.modal_handler_add(self)
        self.report({'INFO'}, "Move handles and press Enter to confirm")
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type in {'RET', 'NUMPAD_ENTER'}:
            self._confirm = True
            return self.finish(context)
        if event.type in {'ESC'}:
            self._confirm = False
            return self.finish(context)
        return {'PASS_THROUGH'}

    def finish(self, context):
        obj = context.active_object
        layer = obj.data.layers.active
        frame = layer.frames.active

        if self._confirm:
            dst = [(h.location.x, h.location.y) for h in self._handles]
            mat = self.compute_matrix(self._src_bbox, dst)
            stroke = frame.strokes[0]
            for p in stroke.points:
                vec = mat @ Vector((p.co.x, p.co.y, 1.0))
                p.co.x = vec.x / vec.z
                p.co.y = vec.y / vec.z

        for h in self._handles:
            bpy.data.objects.remove(h, do_unlink=True)
        self._handles = None
        return {'FINISHED'}

    @staticmethod
    def compute_matrix(src, dst):
        A = []
        B = []
        for (x, y), (u, v) in zip(src, dst):
            A.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
            A.append([0, 0, 0, x, y, 1, -v * x, -v * y])
            B.extend([u, v])
        A = np.array(A)
        B = np.array(B)
        X = np.linalg.solve(A, B)
        M = Matrix(((X[0], X[1], X[2]),
                    (X[3], X[4], X[5]),
                    (X[6], X[7], 1)))
        return M


classes = (
    GREASE_PENCIL_OT_relative_layer_mask_add,
    GREASE_PENCIL_OT_perspective_warp,
)
