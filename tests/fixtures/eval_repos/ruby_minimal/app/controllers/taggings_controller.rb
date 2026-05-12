class TaggingsController < ApplicationController
  def index
    @taggings = Tagging.all
  end
end
