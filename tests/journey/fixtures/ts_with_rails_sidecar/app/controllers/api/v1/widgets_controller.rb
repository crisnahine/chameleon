module Api
  module V1
    class WidgetsController < ApplicationController
      def index
        render json: Widget.all
      end
    end
  end
end
