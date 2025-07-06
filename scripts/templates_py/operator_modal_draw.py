import bpy
import blf
import gpu
from gpu_extras.batch import batch_for_shader

# Store completed strokes and redo stack globally.
strokes = []
redo_stack = []
current_stroke = []


def draw_callback_px(self, context):
    # Draw all stored strokes and the one currently being drawn.
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    gpu.state.line_width_set(2.0)
    shader.uniform_float('color', (0.0, 0.0, 0.0, 0.5))

    for stroke in strokes:
        if len(stroke) >= 2:
            batch = batch_for_shader(shader, 'LINE_STRIP', {'pos': stroke})
            batch.draw(shader)

    if current_stroke:
        batch = batch_for_shader(shader, 'LINE_STRIP', {'pos': current_stroke})
        batch.draw(shader)

    gpu.state.line_width_set(1.0)
    gpu.state.blend_set('NONE')


class ModalDrawOperator(bpy.types.Operator):
    """Draw freehand strokes in the 3D view"""
    bl_idname = "view3d.modal_draw_operator"
    bl_label = "Draw Strokes"

    def modal(self, context, event):
        global current_stroke
        context.area.tag_redraw()

        if event.type in {'RIGHTMOUSE', 'ESC'}:
            bpy.types.SpaceView3D.draw_handler_remove(self._handle, 'WINDOW')
            return {'CANCELLED'}

        if event.type == 'LEFTMOUSE':
            if event.value == 'PRESS':
                current_stroke = []
                strokes.append(current_stroke)
                redo_stack.clear()
            elif event.value == 'RELEASE':
                current_stroke = []
            return {'RUNNING_MODAL'}

        if event.type == 'MOUSEMOVE' and current_stroke is not None:
            current_stroke.append((event.mouse_region_x, event.mouse_region_y))
            return {'RUNNING_MODAL'}

        if event.type == 'Z' and event.value == 'PRESS':
            if strokes:
                redo_stack.append(strokes.pop())
            return {'RUNNING_MODAL'}

        if event.type == 'Y' and event.value == 'PRESS':
            if redo_stack:
                strokes.append(redo_stack.pop())
            return {'RUNNING_MODAL'}

        return {'RUNNING_MODAL'}

    def invoke(self, context, event):
        args = (self, context)
        self._handle = bpy.types.SpaceView3D.draw_handler_add(draw_callback_px, args, 'WINDOW', 'POST_PIXEL')
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}


class StrokeUndo(bpy.types.Operator):
    bl_idname = "view3d.stroke_undo"
    bl_label = "Undo Stroke"

    def execute(self, context):
        if strokes:
            redo_stack.append(strokes.pop())
            context.area.tag_redraw()
        return {'FINISHED'}


class StrokeRedo(bpy.types.Operator):
    bl_idname = "view3d.stroke_redo"
    bl_label = "Redo Stroke"

    def execute(self, context):
        if redo_stack:
            strokes.append(redo_stack.pop())
            context.area.tag_redraw()
        return {'FINISHED'}


class StrokePanel(bpy.types.Panel):
    bl_label = "Stroke Tools"
    bl_idname = "VIEW3D_PT_stroke_tools"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Tool"

    def draw(self, context):
        layout = self.layout
        layout.operator(ModalDrawOperator.bl_idname, text="Draw Stroke")
        layout.separator()
        layout.operator(StrokeUndo.bl_idname, text="Undo")
        layout.operator(StrokeRedo.bl_idname, text="Redo")


classes = (
    ModalDrawOperator,
    StrokeUndo,
    StrokeRedo,
    StrokePanel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
